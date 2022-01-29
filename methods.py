from console import console
import torch
import numpy as np
import logging
from torch.optim import Adam, SGD
from args import support_args
from privacy import GNNBasedNoisySGD, TopMFilter, NoisySGD, PMA, ComposedNoisyMechanism
from torch.utils.data import TensorDataset
from trainer import Trainer
from data import NeighborSampler, PoissonDataLoader
from models import GraphSAGEClassifier, MultiStageClassifier, supported_activations
from torch_geometric.loader import NeighborLoader


@support_args
class GAP:
    supported_dp_levels = {'edge', 'node'}
    supported_perturbations = {'aggr', 'graph'}

    def __init__(self,
                 num_classes,
                 dp_level:      dict(help='level of privacy protection', option='-l', choices=supported_dp_levels) = 'edge',
                 epsilon:       dict(help='DP epsilon parameter', option='-e') = np.inf,
                 delta:         dict(help='DP delta parameter; if "auto", sets a proper value based on data size', option='-d') = 'auto',
                 perturbation:  dict(help='perturbation method', option='-p', choices=supported_perturbations) = 'aggr',
                 hops:          dict(help='number of hops', option='-k') = 2,
                 max_degree:    dict(help='max degree per each node (ignored for edge-level DP)') = 10,
                 hidden_dim:    dict(help='dimension of the hidden layers') = 16,
                 encoder_layers:dict(help='number of encoder MLP layers') = 2,
                 pre_layers:    dict(help='number of pre-combination MLP layers') = 1,
                 post_layers:   dict(help='number of post-combination MLP layers') = 1,
                 combine:       dict(help='combination type of transformed hops', choices=MultiStageClassifier.supported_combinations) = 'cat',
                 activation:    dict(help='type of activation function', choices=supported_activations) = 'selu',
                 dropout:       dict(help='dropout rate (between zero and one)') = 0.0,
                 batch_norm:    dict(help='if True, then model uses batch normalization') = True,
                 optimizer:     dict(help='optimization algorithm', choices=['sgd', 'adam']) = 'adam',
                 learning_rate: dict(help='learning rate', option='--lr') = 0.01,
                 weight_decay:  dict(help='weight decay (L2 penalty)') = 0.0,
                 cpu:           dict(help='if True, then model is trained on CPU') = False,
                 pre_epochs:    dict(help='number of epochs for pre-training') = 100,
                 epochs:        dict(help='number of epochs for training') = 100,
                 batch_size:    dict(help='batch size (if zero, performs full-batch training)') = 0,
                 max_grad_norm: dict(help='maximum norm of the per-sample gradients (ignored for edge-level DP)') = 1.0,
                 use_amp:       dict(help='use automatic mixed precision training') = False,
                 ):

        assert dp_level == 'edge' or perturbation == 'aggr'
        assert dp_level == 'edge' or hops == 0 or max_degree > 0 
        assert dp_level == 'edge' or batch_size > 0
        assert encoder_layers == 0 or pre_epochs > 0

        if dp_level == 'node' and batch_norm:
            logging.warn('batch normalization is not supported for node-level DP, setting it to False')
            batch_norm = False

        if encoder_layers == 0:
            pre_epochs = 0

        self.dp_level = dp_level
        self.epsilon = epsilon
        self.delta = delta
        self.perturbation = perturbation
        self.hops = hops
        self.max_degree = max_degree
        self.encoder_layers = encoder_layers
        self.device = 'cpu' if cpu else 'cuda'
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.optimizer_name = optimizer
        self.pre_epochs = pre_epochs
        self.epochs = epochs
        self.batch_size = batch_size
        self.use_amp = use_amp
        self.max_grad_norm = max_grad_norm
        self.noise_scale = 0.0 # used to save noise calibration results

        self.encoder = MultiStageClassifier(
            num_stages=1,
            hidden_dim=hidden_dim,
            output_dim=num_classes,
            pre_layers=encoder_layers,
            post_layers=1,
            combination_type='cat',
            activation=activation,
            dropout=dropout,
            batch_norm=batch_norm,
        )

        self.classifier = MultiStageClassifier(
            num_stages=hops+1,
            hidden_dim=hidden_dim,
            output_dim=num_classes,
            pre_layers=pre_layers,
            post_layers=post_layers,
            combination_type=combine,
            activation=activation,
            dropout=dropout,
            batch_norm=batch_norm,
        )

        self.reset_parameters()

    def reset_parameters(self):
        self.encoder.reset_parameters()
        self.classifier.reset_parameters()

    def set_training_state(self, pre_train):
        self._pre_train_flag = pre_train

    def init_privacy_mechanisms(self):
        self.pma_mechanism = PMA(noise_scale=self.noise_scale, hops=self.hops)

        if self.perturbation == 'graph':
            self.graph_mechanism = TopMFilter(noise_scale=self.noise_scale)
            mechanism_list = [self.graph_mechanism]
        elif self.dp_level == 'edge':
            mechanism_list = [self.pma_mechanism]
        elif self.dp_level == 'node':
            dataset_size = len(self.data_loader('train').dataset)

            self.pretraining_noisy_sgd = NoisySGD(
                noise_scale=self.noise_scale, 
                dataset_size=dataset_size, 
                batch_size=self.batch_size, 
                epochs=self.pre_epochs,
                max_grad_norm=self.max_grad_norm,
            )

            self.training_noisy_sgd = NoisySGD(
                noise_scale=self.noise_scale, 
                dataset_size=dataset_size, 
                batch_size=self.batch_size, 
                epochs=self.epochs,
                max_grad_norm=self.max_grad_norm,
            )

            mechanism_list = [self.pretraining_noisy_sgd, self.pma_mechanism, self.training_noisy_sgd]


        composed_mech = ComposedNoisyMechanism(
            noise_scale=self.noise_scale,
            mechanism_list=mechanism_list, 
            coeff_list=[1]*len(mechanism_list)
        )

        with console.status('calibrating noise to privacy budget'):
            if self.delta == 'auto':
                if np.isinf(self.epsilon):
                    self.delta = 0.0
                else:
                    data_size = self.data.num_edges if self.dp_level == 'edge' else self.data.num_nodes
                    self.delta = 1. / (10 ** len(str(data_size)))
                logging.info('delta = %.0e', self.delta)
            self.noise_scale = composed_mech.calibrate(eps=self.epsilon, delta=self.delta)
            logging.info(f'noise scale: {self.noise_scale:.4f}\n')

    def fit(self, data):
        self.data = data
        self.init_privacy_mechanisms()
        
        with console.status(f'moving data to {self.device}'):
            self.data.to(self.device)

        logging.info('pretraining encoder module...')
        self.pretrain_encoder()

        with console.status('precomputing aggregation module'):
            self.precompute_aggregations()

        logging.info('training classification module...')
        return self.train_classifier()

    def pretrain_encoder(self):
        if self.encoder_layers > 0:
            self.set_training_state(pre_train=True)
            self.encoder.to(self.device)
            self.data.x = torch.stack([self.data.x], dim=-1)

            trainer = Trainer(
                epochs=self.pre_epochs, 
                use_amp=self.use_amp, 
                monitor='val/acc', monitor_mode='max', 
                device=self.device,
                dp_mechanism=self.pretraining_noisy_sgd if self.dp_level == 'node' else None,
            )

            trainer.fit(
                model=self.encoder,
                optimizer=self.configure_optimizers(self.encoder), 
                train_dataloader=self.data_loader('train'), 
                val_dataloader=self.data_loader('val'),
                test_dataloader=None,
                checkpoint=True
            )

            self.encoder = trainer.load_best_model()
            self.data.x = self.encoder.encode(self.data.x)
            self.encoder.to('cpu')

    def precompute_aggregations(self):
        if self.dp_level == 'node' and self.hops > 0:
            self.data = NeighborSampler(self.max_degree)(self.data)
        elif self.perturbation == 'graph':
            self.pma_mechanism.update(noise_scale=0)
            self.data = self.graph_mechanism(self.data)

        sensitivity = 1 if self.dp_level == 'edge' else np.sqrt(self.max_degree)
        self.data = self.pma_mechanism(self.data, sensitivity=sensitivity)
        self.data.to('cpu', 'adj_t')

    def train_classifier(self):
        self.set_training_state(pre_train=False)
        self.classifier.to(self.device)

        trainer = Trainer(
            epochs=self.epochs, 
            use_amp=self.use_amp, 
            monitor='val/acc', monitor_mode='max', 
            device=self.device,
            dp_mechanism=self.training_noisy_sgd if self.dp_level == 'node' else None,
        )

        metrics = trainer.fit(
            model=self.classifier, 
            optimizer=self.configure_optimizers(self.classifier),
            train_dataloader=self.data_loader('train'), 
            val_dataloader=self.data_loader('val'),
            test_dataloader=self.data_loader('test'),
            checkpoint=False,
        )

        self.classifier.to('cpu')
        return metrics

    def data_loader(self, stage):
        mask = self.data[f'{stage}_mask']
        x = self.data.x[mask]
        y = self.data.y[mask]
        dataset = TensorDataset(x, y)
        batch_size = len(dataset) if self.batch_size == 0 else self.batch_size
        return PoissonDataLoader(dataset, batch_size=batch_size)

    def configure_optimizers(self, model):
        Optim = {'sgd': SGD, 'adam': Adam}[self.optimizer_name]
        return Optim(model.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay)


@support_args
class GraphSAGEModel:
    supported_dp_levels = {'edge', 'node'}

    def __init__(self,
                 num_classes,
                 dp_level:      dict(help='level of privacy protection', option='-l', choices=supported_dp_levels) = 'edge',
                 epsilon:       dict(help='DP epsilon parameter', option='-e') = np.inf,
                 delta:         dict(help='DP delta parameter; if "auto", sets a proper value based on data size', option='-d') = 'auto',
                 max_degree:    dict(help='max degree per each node (ignored for edge-level DP)') = 0,
                 hidden_dim:    dict(help='dimension of the hidden layers') = 16,
                 encoder_layers:dict(help='number of encoder MLP layers') = 2,
                 mp_layers:     dict(help='number of GNN layers') = 1,
                 post_layers:   dict(help='number of post-processing MLP layers') = 1,
                 activation:    dict(help='type of activation function', choices=supported_activations) = 'selu',
                 dropout:       dict(help='dropout rate (between zero and one)') = 0.0,
                 batch_norm:     dict(help='if True, then model uses batch normalization') = True,
                 optimizer:     dict(help='optimization algorithm', choices=['sgd', 'adam']) = 'adam',
                 learning_rate: dict(help='learning rate', option='--lr') = 0.01,
                 weight_decay:  dict(help='weight decay (L2 penalty)') = 0.0,
                 cpu:           dict(help='if True, then model is trained on CPU') = False,
                 epochs:        dict(help='number of epochs for training') = 100,
                 batch_size:    dict(help='batch size (if zero, performs full-batch training)') = 0,
                 max_grad_norm: dict(help='maximum norm of the per-sample gradients (ignored for edge-level DP)') = 1.0,
                 use_amp:       dict(help='use automatic mixed precision training') = False,
                 ):

        assert mp_layers >= 1
        assert dp_level == 'edge' or mp_layers == 1
        assert dp_level == 'edge' or max_degree > 0 
        assert dp_level == 'edge' or batch_size > 0

        if dp_level == 'node' and batch_norm:
            logging.warn('batch normalization is not supported for node-level DP, setting it to False')
            batch_norm = False

        self.dp_level = dp_level
        self.epsilon = epsilon
        self.delta = delta
        self.max_degree = max_degree
        self.encoder_layers = encoder_layers
        self.mp_layers = mp_layers
        self.post_layers = post_layers
        self.device = 'cpu' if cpu else 'cuda'
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.optimizer_name = optimizer
        self.epochs = epochs
        self.batch_size = batch_size
        self.max_grad_norm = max_grad_norm
        self.use_amp = use_amp
        self.noise_scale = 0.0 # used to save noise calibration results

        self.classifier = GraphSAGEClassifier(
            hidden_dim=hidden_dim, 
            output_dim=num_classes, 
            pre_layers=encoder_layers,
            mp_layers=mp_layers, 
            post_layers=post_layers, 
            activation=activation, 
            dropout=dropout, 
            batch_norm=batch_norm,
        )

        self.reset_parameters()

    def reset_parameters(self):
        self.classifier.reset_parameters()

    def init_privacy_mechanisms(self):
        if self.dp_level == 'edge':
            mech = TopMFilter(noise_scale=self.noise_scale)
            self.graph_mechanism = mech
        else:
            mech = GNNBasedNoisySGD(
                noise_scale=self.noise_scale, 
                dataset_size=self.data.train_mask.sum().item(),
                batch_size=self.batch_size, 
                epochs=self.epochs,
                max_grad_norm=self.max_grad_norm,
                max_degree=self.max_degree,
            )
            self.training_noisy_sgd = mech

        with console.status('calibrating noise to privacy budget'):
            if self.delta == 'auto':
                if np.isinf(self.epsilon):
                    self.delta = 0.0
                else:
                    self.delta = 1. / (10 ** len(str(self.data.num_edges)))
                logging.info('delta = %.0e', self.delta)
            self.noise_scale = mech.calibrate(eps=self.epsilon, delta=self.delta)
            logging.info(f'noise scale: {self.noise_scale:.4f}\n')

    def fit(self, data):
        self.data = data
        self.init_privacy_mechanisms()
        
        with console.status(f'moving data to {self.device}'):
            self.data.to(self.device)

        if self.dp_level == 'node':
            with console.status('bounding the number of neighbors per node'):
                self.data = NeighborSampler(self.max_degree)(self.data)
        else:
            with console.status('perturbing graph structure'):
                self.data = self.graph_mechanism(self.data)

        logging.info('training classifier...')
        return self.train_classifier()

    def train_classifier(self):
        self.classifier.to(self.device)

        trainer = Trainer(
            epochs=self.epochs, 
            use_amp=self.use_amp, 
            monitor='val/acc', monitor_mode='max', 
            device=self.device,
            dp_mechanism=self.training_noisy_sgd if self.dp_level == 'node' else None,
        )

        metrics = trainer.fit(
            model=self.classifier, 
            optimizer=self.configure_optimizers(self.classifier),
            train_dataloader=self.data_loader('train'), 
            val_dataloader=self.data_loader('val'),
            test_dataloader=self.data_loader('test'),
            checkpoint=False,
        )

        return metrics

    def data_loader(self, stage):
        if self.batch_size == 0:
            self.data.batch_size = self.data.num_nodes
            return [self.data]
        else:
            num_neighbors = self.max_degree if self.max_degree > 0 else -1
            return NeighborLoader(
                data=self.data, 
                num_neighbors=[num_neighbors]*self.mp_layers, 
                input_nodes=self.data[f'{stage}_mask'],
                replace=False,
                batch_size=self.batch_size,
                shuffle=True,
                num_workers=6,
            )


    def configure_optimizers(self, model):
        Optim = {'sgd': SGD, 'adam': Adam}[self.optimizer_name]
        return Optim(model.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay)
