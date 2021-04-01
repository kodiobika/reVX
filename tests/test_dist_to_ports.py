# -*- coding: utf-8 -*-
"""
Distance to Ports tests
"""
from click.testing import CliRunner
import json
import numpy as np
import os
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest
import shutil
import tempfile
import traceback

from rex.resource import Resource
from rex.utilities.loggers import LOGGERS
from rex.utilities.utilities import get_lat_lon_cols
from reV.handlers.exclusions import ExclusionLayers
from reVX import TESTDATADIR
from reVX.offshore.dist_to_ports import DistanceToPorts
from reVX.offshore.dist_to_ports_cli import main
from reVX.utilities.utilities import coordinate_distance

EXCL_H5 = os.path.join(TESTDATADIR, 'offshore', 'offshore.h5')
PORTS_FPATH = os.path.join(TESTDATADIR, 'offshore', 'ports',
                           'ports_operations.shp')
ASSEMBLY_AREAS = os.path.join(TESTDATADIR, 'offshore', 'assembly_areas.csv')


def get_dist_to_ports(excl_h5, ports_layer='ports_operations'):
    """
    Extract "truth" distance to ports layer from exclusion .h5 file
    """
    with ExclusionLayers(excl_h5) as f:
        dist_to_ports = f[ports_layer]

    return dist_to_ports


def get_assembly_areas(excl_h5, assembly_dset='assembly_areas'):
    """
    Extract "truth" assembly areas table
    """
    with Resource(excl_h5) as f:
        assembly_areas = f.df_str_decode(pd.DataFrame(f[assembly_dset]))

    return assembly_areas


@pytest.fixture(scope="module")
def runner():
    """
    cli runner
    """
    return CliRunner()


def test_haversine_versus_dist_to_port():
    """
    Compare distance to points versus haversine distance
    """
    dtp = DistanceToPorts(PORTS_FPATH, EXCL_H5)
    cols = get_lat_lon_cols(dtp.ports)

    with ExclusionLayers(EXCL_H5) as f:
        lat = f.latitude
        lon = f.longitude

    pixel_coords = np.dstack((lat.ravel(), lon.ravel()))[0]

    hav_dist = np.full(lat.shape, np.finfo('float32').max, dtype='float32')
    for i, port in dtp.ports.iterrows():
        port_idx = port[['row', 'col']].values
        port_dist = port['dist_to_pixel']
        port_coords = np.expand_dims(port[cols].values, 0).astype('float32')
        h_dist = \
            coordinate_distance(port_coords, pixel_coords).reshape(lat.shape)
        l_dist = dtp._lc_dist_to_port(EXCL_H5, port_idx, port_dist)

        err = (l_dist - h_dist) / h_dist
        msg = ("Haversine distance is greater than least cost distance for "
               "port {}!".format(i))
        assert np.all(err > -0.05), msg

        hav_dist = np.minimum(hav_dist, h_dist)

    test = dtp.least_cost_distance(max_workers=1)
    mask = test != -1
    err = (test[mask] - hav_dist[mask]) / hav_dist[mask]
    msg = "Haversine distance is greater than distance to closest port!"
    assert np.all(err > -0.05), msg


@pytest.mark.parametrize('max_workers', [None, 1])
def test_dist_to_ports(max_workers):
    """
    Compute distance to ports
    """
    baseline = get_dist_to_ports(EXCL_H5)
    test = DistanceToPorts.run(PORTS_FPATH, EXCL_H5, max_workers=max_workers)

    msg = 'distance to ports does not match baseline distances'
    assert np.allclose(baseline, test), msg


@pytest.mark.parametrize('ports_layer', [None, 'test'])
def test_cli(runner, ports_layer):
    """
    Test CLI
    """
    update = False
    if ports_layer is None:
        update = True
        ports_layer = 'ports_operations'

    with tempfile.TemporaryDirectory() as td:
        excl_fpath = os.path.basename(EXCL_H5)
        excl_fpath = os.path.join(td, excl_fpath)
        shutil.copy(EXCL_H5, excl_fpath)
        config = {
            "directories": {
                "log_directory": td,
            },
            "execution_control": {
                "option": "local"
            },
            "excl_fpath": excl_fpath,
            "ports_fpath": PORTS_FPATH,
            "output_dist_layer": ports_layer,
            "update": update,
            "assembly_areas": ASSEMBLY_AREAS
        }
        config_path = os.path.join(td, 'config.json')
        with open(config_path, 'w') as f:
            json.dump(config, f)

        result = runner.invoke(main, ['from-config',
                                      '-c', config_path])
        msg = ('Failed with error {}'
               .format(traceback.print_exception(*result.exc_info)))
        assert result.exit_code == 0, msg

        baseline = get_dist_to_ports(EXCL_H5)
        test = get_dist_to_ports(excl_fpath, ports_layer=ports_layer)

        msg = 'distance to ports does not match baseline distances'
        assert np.allclose(baseline, test), msg

        truth = get_assembly_areas(EXCL_H5)
        test = get_assembly_areas(excl_fpath)
        assert_frame_equal(truth, test, check_dtype=False)

    LOGGERS.clear()


def plot():
    """
    Plot least cost distance vs haversine distance
    """
    import matplotlib.pyplot as plt

    dtp = DistanceToPorts(PORTS_FPATH, EXCL_H5)
    test = dtp.least_cost_distance(max_workers=1)
    mask = test != -1

    cols = get_lat_lon_cols(dtp.ports)

    with ExclusionLayers(EXCL_H5) as f:
        lat = f.latitude
        lon = f.longitude

    pixel_coords = np.dstack((lat.ravel(), lon.ravel()))[0]

    for _, port in dtp.ports.iterrows():
        port_idx = port[['row', 'col']].values
        port_dist = port['dist_to_pixel']
        port_coords = np.expand_dims(port[cols].values, 0).astype('float32')
        h_dist = \
            coordinate_distance(port_coords, pixel_coords).reshape(lat.shape)
        l_dist = dtp._lc_dist_to_port(EXCL_H5, port_idx, port_dist)

        print(port)
        plt.imshow(mask)
        plt.plot(port_idx[1], port_idx[0], 'ro')
        plt.colorbar()
        plt.show()

        vmax = l_dist[mask].max()
        plt.imshow(l_dist, vmin=0, vmax=vmax, cmap='viridis')
        plt.plot(port_idx[1], port_idx[0], 'ko')
        plt.colorbar()
        plt.show()

        diff = l_dist - h_dist
        plt.imshow(diff, vmin=np.min(diff), vmax=0.09)
        plt.plot(port_idx[1], port_idx[0], 'ro')
        plt.colorbar()
        plt.show()

        err = diff / h_dist
        vmax = err[mask].max()
        vmax = 0
        plt.imshow(err, vmin=-0.05, vmax=vmax, cmap='rainbow')
        plt.plot(port_idx[1], port_idx[0], 'ko')
        plt.colorbar()
        plt.show()


def execute_pytest(capture='all', flags='-rapP'):
    """Execute module as pytest with detailed summary report.

    Parameters
    ----------
    capture : str
        Log or stdout/stderr capture option. ex: log (only logger),
        all (includes stdout/stderr)
    flags : str
        Which tests to show logs and results for.
    """

    fname = os.path.basename(__file__)
    pytest.main(['-q', '--show-capture={}'.format(capture), fname, flags])


if __name__ == '__main__':
    execute_pytest()
