# -*- coding: utf-8 -*-
"""
Least cost transmission line path tests
"""
from click.testing import CliRunner
import json
import numpy as np
import os
import pytest
import tempfile
import traceback

from rex.utilities.loggers import LOGGERS

from reV.handlers.exclusions import ExclusionLayers

from reVX import TESTDATADIR
from reVX.cli import main as cli
from reVX.least_cost_xmission.cost_creator_cli import main
from reVX.least_cost_xmission.cost_creator import XmissionCostCreator, \
    XmissionConfig
from reVX.least_cost_xmission.config import NLCD_LAND_USE_CLASSES, \
    TEST_DEFAULT_MULTS

RI_DATA_DIR = os.path.join(TESTDATADIR, 'ri_exclusions')
EXCL_H5 = os.path.join(RI_DATA_DIR, 'ri_exclusions.h5')
ISO_REGIONS_F = os.path.join(RI_DATA_DIR, 'ri_iso_regions.tif')


def build_test_costs():
    """
    Build test costs
    """
    XmissionCostCreator.run(EXCL_H5, ISO_REGIONS_F,
                            slope_layer='ri_srtm_slope', nlcd_layer='ri_nlcd',
                            tiff_dir=None, default_mults=TEST_DEFAULT_MULTS)


@pytest.fixture(scope="module")
def runner():
    """
    cli runner
    """
    return CliRunner()


def test_land_use_multiplier():
    """ Test land use multiplier creation """
    lu_mults = {'forest': 1.63, 'wetland': 1.5}
    arr = np.array([[[0, 95, 90], [42, 41, 15]]])
    xcc = XmissionCostCreator(EXCL_H5, ISO_REGIONS_F)
    out = xcc._compute_land_use_mult(arr, lu_mults, NLCD_LAND_USE_CLASSES)
    expected = np.array([[[1.0, 1.5, 1.5], [1.63, 1.63, 1.0]]],
                        dtype=np.float32)
    assert np.array_equal(out, expected)


def test_slope_multiplier():
    """ Test slope multiplier creation """
    arr = np.array([[[0, 1, 10], [20, 1, 6]]])
    config = {'hill_mult': 1.2, 'mtn_mult': 1.5,
              'hill_slope': 2, 'mtn_slope': 8}
    xcc = XmissionCostCreator(EXCL_H5, ISO_REGIONS_F)
    out = xcc._compute_slope_mult(arr, config)
    expected = np.array([[[1.0, 1.0, 1.5], [1.5, 1.0, 1.2]]],
                        dtype=np.float32)
    assert np.array_equal(out, expected)


def test_full_costs_workflow():
    """
    Test full cost calculator workflow for RI against known costs
    """
    xc = XmissionConfig()

    xcc = XmissionCostCreator(EXCL_H5, ISO_REGIONS_F,
                              iso_lookup=xc['iso_lookup'])

    mults_arr = xcc.compute_multipliers(xc['iso_multipliers'], excl_h5=None,
                                        slope_layer='ri_srtm_slope',
                                        nlcd_layer='ri_nlcd',
                                        land_use_classes=NLCD_LAND_USE_CLASSES,
                                        default_mults=TEST_DEFAULT_MULTS)

    for _, capacity in xc['power_classes'].items():
        with ExclusionLayers(EXCL_H5) as el:
            known_costs = el['tie_line_costs_{}MW'.format(capacity)]

        blc_arr = xcc.compute_base_line_costs(capacity,
                                              xc['base_line_costs'])
        costs_arr = blc_arr * mults_arr
        assert np.isclose(known_costs, costs_arr).all()


def test_cli(runner):
    """
    Test CostCreator CLI
    """

    with tempfile.TemporaryDirectory() as td:
        layers = {'layers':
                  {'ri_srtm_slope': os.path.join(TESTDATADIR, 'ri_exclusions',
                                                 'ri_srtm_slope.tif'),
                   'ri_nlcd': os.path.join(TESTDATADIR, 'ri_exclusions',
                                           'ri_nlcd.tif')}}
        layers_path = os.path.join(td, 'layers.json')
        with open(layers_path, 'w') as f:
            json.dump(layers, f)

        excl_h5 = os.path.join(td, "test.h5")
        result = runner.invoke(cli, ['exclusions',
                                     '-h5', excl_h5,
                                     'layers-to-h5',
                                     '-l', layers_path])
        msg = ('Failed with error {}'
               .format(traceback.print_exception(*result.exc_info)))
        assert result.exit_code == 0, msg

        mults_path = os.path.join(td, 'default_mults.json')
        with open(mults_path, 'w') as f:
            json.dump(TEST_DEFAULT_MULTS, f)

        config = {
            "directories": {
                "log_directory": td,
                "output_directory": td
            },
            "execution_control": {
                "option": "local",
            },
            "h5_fpath": excl_h5,
            "iso_regions": ISO_REGIONS_F,
            "slope_layer": 'ri_srtm_slope',
            "nlcd_layer": 'ri_nlcd',
            "default_mults": mults_path
        }
        config_path = os.path.join(td, 'config.json')
        with open(config_path, 'w') as f:
            json.dump(config, f)

        result = runner.invoke(main, ['from-config',
                                      '-c', config_path])
        msg = ('Failed with error {}'
               .format(traceback.print_exception(*result.exc_info)))
        assert result.exit_code == 0, msg

        with ExclusionLayers(EXCL_H5) as f_truth:
            with ExclusionLayers(excl_h5) as f_test:
                for layer in f_test.layers:
                    test = f_test[layer]
                    truth = f_truth[layer]

                    assert np.allclose(truth, test)

    LOGGERS.clear()


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
