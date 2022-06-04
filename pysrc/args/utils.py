from typing import Annotated, Callable, Literal, Union, get_args, get_origin
from pysrc.console import console
import math
import inspect
from rich.table import Table
from rich.highlighter import ReprHighlighter
from rich import box
from tabulate import tabulate
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from pysrc.utils import RT


ArgType = Union[Namespace, dict[str, object]]

class ArgWithLiteral:
    def __init__(self, main_type, literals):
        self.main_type = main_type
        self.literals = literals

    def __call__(self, arg):
        try:
            return self.main_type(arg)
        except ValueError:
            if arg in self.literals:
                return arg
            else:
                raise ArgumentTypeError(f'{arg} is not a valid literal')

def boolean(v: Union[str, bool]) -> bool:
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1', 'on'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0', 'off'):
        return False
    else:
        raise ArgumentTypeError('Boolean value expected.')


def strip_unexpected_kwargs(callable: Callable, kwargs: dict) -> dict[str, object]:
    signature = inspect.signature(callable)
    parameters = signature.parameters
    out_kwargs = {arg: value for arg, value in kwargs.items() if arg in parameters}

    # check if the function has kwargs
    for _, param in parameters.items():
        annotation = param.annotation
        if get_origin(annotation) is Annotated:
            metadata = get_args(annotation)[1]
            bases = metadata.get('bases', [])
            for base_callable in bases:
                out_kwargs.update(strip_unexpected_kwargs(base_callable, kwargs))
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            return kwargs

    return out_kwargs


def invoke(callable: Callable[..., RT], **kwargs) -> RT:
    kwargs = strip_unexpected_kwargs(callable, kwargs)
    return callable(**kwargs)


def create_arguments(callable: Callable, parser: ArgumentParser, exclude: list = []) -> list[str]:
    arguments_added = []
    parameters = inspect.signature(callable).parameters

    # iterate over the parameters
    for param_name, param_obj in parameters.items():

        # skip the parameters that are in the exclude list
        if param_name in exclude:
            continue
        
        # get the annotation
        annot_obj = param_obj.annotation

        # only process annotated parameters
        if get_origin(annot_obj) is Annotated:

            # extract parameter type and metadata from annotation
            annotation = get_args(annot_obj)
            param_type = annotation[0]
            metadata: dict = annotation[1]

            # get the base callable arguments
            bases = metadata.get('bases', False)

            if bases:
                # if there are base callables, recursively add their args to the parser
                for base_callable in bases:
                    arguments_added += create_arguments(
                        callable=base_callable, 
                        parser=parser, 
                        exclude=metadata.get('exclude', []) + arguments_added
                    )
            else:
                # if there are no base callables, add the parameter to the parser
                metadata['type'] = param_type
                metadata['dest'] = param_name

                # if the parameter has a default value, add it to the parser
                # otherwise, set the parameter as required
                if param_obj.default is not inspect.Parameter.empty:
                    metadata['default'] = param_obj.default
                else:
                    metadata['required'] = True
                    metadata['default'] = 'required'

                # tweak specific data types
                if param_type is bool:
                    # process boolean parameters
                    metadata['type'] = boolean
                    metadata['nargs'] = '?'
                    metadata['const'] = True
                elif get_origin(param_type) is Union:
                    # process union parameters
                    sub_types = get_args(param_type)
                    if len(sub_types) == 2 and get_origin(sub_types[0]) is Literal:
                        metadata['type'] = ArgWithLiteral(main_type=sub_types[1], literals=get_args(sub_types[0]))
                        metadata['metavar'] = f"<{sub_types[1].__name__}>" + '|{' +  ','.join(map(str, get_args(sub_types[0]))) + '}'

                # if metadata contains "choices", the parser uses that as meta variable
                # otherwise, if "metavar" is not provided, set the meta variable to  <parameter type>
                if 'choices' not in metadata:
                    try:
                        metadata['metavar'] = metadata.get('metavar', f'<{param_type.__name__}>')
                    except: pass

                # create options based on parameter name
                options = {f'--{param_name}', f'--{param_name.replace("_", "-")}'}

                # add custome options if provided
                custom_options = metadata.pop('option', [])
                custom_options = [custom_options] if isinstance(custom_options, str) else custom_options
                options.update(custom_options)

                # sort option names based on their length
                options = sorted(sorted(list(options)), key=len)

                # add the parameter to the parser
                parser.add_argument(*options, **metadata)
                arguments_added.append(param_name)
    
    return arguments_added


def print_args(args: ArgType, num_cols: int = 4):
    args = args if isinstance(args, dict) else vars(args)
    num_args = len(args)
    num_rows = math.ceil(num_args / num_cols)
    col = 0
    data = {}
    keys = []
    vals = []

    for i, (key, val) in enumerate(args.items()):
        keys.append(f'{key}:')
        
        vals.append(val)
        if (i + 1) % num_rows == 0:
            data[col] = keys
            data[col+1] = vals
            keys = []
            vals = []
            col += 2

    data[col] = keys
    data[col+1] = vals

    highlighter = ReprHighlighter()
    message = tabulate(data, tablefmt='plain')
    table = Table(title='program arguments', show_header=False, box=box.HORIZONTALS)
    table.add_row(highlighter(message))

    console.print()
    console.log(table)
    console.print()
