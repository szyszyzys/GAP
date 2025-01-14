#!/bin/bash

datasets='flickr lastfm'

epss='1.0 2.0 3.0 4.0 5.0 6.0 7.0 8.0'

Run_attack() {
  python train.py "gap-edp" --dataset $1  -e $2 --hops 4
}

for dataset in $datasets; do
      for eps in $epss; do
            Run_attack "$dataset" "$eps"
            mv ./checkpoints/save.pt ./checkpoints/"$dataset"_eps_"$eps"_edp.pt
            mv ./checkpoints/encoder/save.pt ./checkpoints/encoder/"$dataset"_eps_"$eps"_edp.pt
    done
done
