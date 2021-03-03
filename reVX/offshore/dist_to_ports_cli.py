# -*- coding: utf-8 -*-
"""
Distance to Ports Command Line Interface
"""
import click
import logging
import os

from rex.utilities.loggers import init_mult
from rex.utilities.cli_dtypes import STR, INT
from rex.utilities.hpc import SLURM
from rex.utilities.utilities import get_class_properties

from reVX.config.dist_to_ports import DistToPortsConfig
from reVX.offshore.dist_to_ports import DistanceToPorts
from reVX import __version__

logger = logging.getLogger(__name__)


@click.group()
@click.option('--name', '-n', default='DistToPorts', type=STR,
              show_default=True,
              help='Job name.')
@click.option('--verbose', '-v', is_flag=True,
              help='Flag to turn on debug logging. Default is not verbose.')
@click.pass_context
def main(ctx, name, verbose):
    """
    Distance to Ports Command Line Interface
    """
    ctx.ensure_object(dict)
    ctx.obj['VERBOSE'] = verbose
    ctx.obj['NAME'] = name


@main.command()
def valid_config_keys():
    """
    Echo the valid Distance to Port config keys
    """
    click.echo(', '.join(get_class_properties(DistToPortsConfig)))


@main.command()
def version():
    """
    print version
    """
    click.echo(__version__)


def run_local(ctx, config):
    """
    Compute distance to ports locally using config

    Parameters
    ----------
    ctx : click.ctx
        click ctx object
    config : reVX.config.dist_to_ports.DistToPortsConfig
        Distance to ports config object.
    """
    ctx.obj['NAME'] = config.name
    ctx.invoke(local,
               ports_fpath=config.ports_fpath,
               excl_fpath=config.excl_fpath,
               dist_layer=config.dist_layer,
               ports_layer=config.ports_layer,
               max_workers=config.max_workers,
               update_layer=config.update_layer,
               log_dir=config.logdir,
               verbose=config.log_level)


@main.command()
@click.option('--config', '-c', required=True,
              type=click.Path(exists=True),
              help='Filepath to MeanWindDirections config json file.')
@click.option('--verbose', '-v', is_flag=True,
              help='Flag to turn on debug logging. Default is not verbose.')
@click.pass_context
def from_config(ctx, config, verbose):
    """
    Compute distance to ports from a config.
    """

    config = DistToPortsConfig(config)

    if 'VERBOSE' in ctx.obj:
        if any((ctx.obj['VERBOSE'], verbose)):
            config._log_level = logging.DEBUG
    elif verbose:
        config._log_level = logging.DEBUG

    if config.execution_control.option == 'local':
        run_local(ctx, config)

    if config.execution_control.option == 'eagle':
        eagle(config)


@main.command()
@click.option('--ports_fpath', '-ports', required=True,
              type=click.Path(exists=True),
              help=("Path to shape file containing ports to compute least "
                    "cost distance to"))
@click.option('--excl_fpath', '-excl', required=True,
              type=click.Path(exists=True),
              help="Filepath to exclusions h5 with techmap dataset.")
@click.option('--dist_layer', '-dl', default='dist_to_coast',
              show_default=True,
              help=("Exclusions layer with distance to coast values"))
@click.option('--ports_layer', '-pl', default=None, type=STR,
              show_default=True,
              help=("Exclusion layer under which the distance to ports layer "
                    "should be saved, if None use the ports file-name"))
@click.option('--max_workers', '-mw', default=None, type=INT,
              show_default=True,
              help=(" Number of workers to use for setback computation, if 1 "
                    "run in serial, if > 1 run in parallel with that many "
                    "workers, if None run in parallel on all available cores"))
@click.option('--update_layer', '-u', is_flag=True,
              help=("Flag to check for an existing distance to port layer and "
                    "update it with new least cost distances to new ports, if "
                    "None compute the least cost distance from scratch"))
@click.option('--log_dir', '-log', default=None, type=STR,
              show_default=True,
              help='Directory to dump log files. Default is ports_layer.')
@click.option('--verbose', '-v', is_flag=True,
              help='Flag to turn on debug logging. Default is not verbose.')
@click.pass_context
def local(ctx, ports_fpath, excl_fpath, dist_layer, ports_layer,
          max_workers, update_layer, log_dir, verbose):
    """
    Compute distance to ports on local hardware
    """
    name = ctx.obj['NAME']
    if 'VERBOSE' in ctx.obj:
        verbose = any((ctx.obj['VERBOSE'], verbose))

    log_modules = [__name__, 'reVX', 'reV', 'rex']
    init_mult(name, log_dir, modules=log_modules, verbose=verbose)

    logger.info('Computing distance to ports in {} \n'
                'Outputs to be stored in: {}'.format(ports_fpath, excl_fpath))

    DistanceToPorts.run(ports_fpath, excl_fpath,
                        dist_layer=dist_layer, ports_layer=ports_layer,
                        chunks=(128, 128), max_workers=max_workers,
                        update_layer=update_layer)


def get_node_cmd(config):
    """
    Get the node CLI call for distance to ports computation.

    Parameters
    ----------
    config : reVX.config.dist_to_ports.DistToPortsConfig
        Distance to ports config object.

    Returns
    -------
    cmd : str
        CLI call to submit to SLURM execution.
    """

    args = ['-n {}'.format(SLURM.s(config.name)),
            'local',
            '-ports {}'.format(SLURM.s(config.ports_fpath)),
            '-excl {}'.format(SLURM.s(config.excl_fpath)),
            '-dl {}'.format(SLURM.s(config.dist_layer)),
            '-pl {}'.format(SLURM.s(config.ports_layer)),
            '-mw {}'.format(SLURM.s(config.max_workers)),
            '-log {}'.format(SLURM.s(config.logdir)),
            ]

    if config.update_layer:
        args.append('-u')

    if config.log_level == logging.DEBUG:
        args.append('-v')

    cmd = ('python -m reVX.offshore.dist_to_ports_cli {}'
           .format(' '.join(args)))
    logger.debug('Submitting the following cli call:\n\t{}'.format(cmd))

    return cmd


def eagle(config):
    """
    Compute distance to ports on Eagle HPC.

    Parameters
    ----------
    config : reVX.config.dist_to_ports.DistToPortsConfig
        Distance to ports config object.
    """

    cmd = get_node_cmd(config)
    name = config.name
    log_dir = config.logdir
    stdout_path = os.path.join(log_dir, 'stdout/')

    slurm_manager = SLURM()

    logger.info('Compute distance to ports on Eagle with '
                'node name "{}"'.format(name))
    out = slurm_manager.sbatch(cmd,
                               alloc=config.execution_control.allocation,
                               memory=config.execution_control.memory,
                               walltime=config.execution_control.walltime,
                               feature=config.execution_control.feature,
                               name=name, stdout_path=stdout_path,
                               conda_env=config.execution_control.conda_env,
                               module=config.execution_control.module)[0]
    if out:
        msg = ('Kicked off distance to ports calculation "{}" '
               '(SLURM jobid #{}) on Eagle.'
               .format(name, out))
    else:
        msg = ('Was unable to kick off distance to ports calculation '
               '"{}". Please see the stdout error messages'
               .format(name))

    click.echo(msg)
    logger.info(msg)


if __name__ == '__main__':
    try:
        main(obj={})
    except Exception:
        logger.exception('Error running distance to ports CLI')
        raise
