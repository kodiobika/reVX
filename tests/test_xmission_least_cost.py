# -*- coding: utf-8 -*-
"""
Least cost transmission line path tests
"""
from click.testing import CliRunner
import json
import numpy as np
import os
import pandas as pd
import pytest
import tempfile
import traceback

from rex.utilities.loggers import LOGGERS
from reVX import TESTDATADIR
from reVX.least_cost_xmission.least_cost_xmission_cli import main
from reVX.least_cost_xmission.least_cost_xmission import LeastCostXmission

COST_H5 = os.path.join(TESTDATADIR, 'xmission', 'xmission_layers.h5')
FEATURES = os.path.join(TESTDATADIR, 'xmission', 'ri_allconns.gpkg')
CHECK_COLS = ('raw_line_cost', 'dist_km', 'length_mult', 'tie_line_cost',
              'xformer_cost_per_mw', 'xformer_cost', 'sub_upgrade_cost',
              'new_sub_cost', 'connection_cost', 'trans_cap_cost', 'trans_gid',
              'sc_point_gid')
N_SC_POINTS = 10  # number of sc_points to run, chosen at random for each test


def check(truth, test, check_cols=CHECK_COLS):
    """
    Compare values in truth and test for given columns
    """
    if check_cols is None:
        check_cols = truth.columns.values

    for c in check_cols:
        msg = f'values for {c} do not match!'
        assert np.allclose(truth[c].values, test[c].values,
                           equal_nan=True), msg


@pytest.fixture(scope="module")
def runner():
    """
    cli runner
    """
    return CliRunner()


@pytest.mark.parametrize('capacity', [100, 200, 400, 1000])
def test_capacity_class(capacity):
    """
    Test least cost xmission and compare with baseline data
    """
    truth = os.path.join(TESTDATADIR, 'xmission',
                         f'least_cost_{capacity}MW.csv')
    if not os.path.exists(truth):
        sc_point_gids = None
    else:
        truth = pd.read_csv(truth)
        sc_point_gids = truth['sc_point_gid'].unique()
        sc_point_gids = np.random.choice(sc_point_gids,
                                         size=N_SC_POINTS, replace=False)

    test = LeastCostXmission.run(COST_H5, FEATURES, capacity,
                                 sc_point_gids=sc_point_gids)

    if not isinstance(truth, pd.DataFrame):
        test.to_csv(truth, index=False)
        truth = pd.read_csv(truth)

    check(truth, test)


def test():
    """
    Test least cost xmission and compare with baseline data
    """
    truth = os.path.join(TESTDATADIR, 'xmission', 'least_cost_200MW.csv')
    if not os.path.exists(truth):
        sc_point_gids = None
    else:
        truth = pd.read_csv(truth)
        sc_point_gids = truth['sc_point_gid'].unique()
        sc_point_gids = np.random.choice(sc_point_gids,
                                         size=N_SC_POINTS, replace=False)

    test = LeastCostXmission.run(COST_H5, FEATURES, 100,
                                 max_workers=1,
                                 sc_point_gids=sc_point_gids)

    if not isinstance(truth, pd.DataFrame):
        test.to_csv(truth, index=False)
        truth = pd.read_csv(truth)

    check(truth, test)


@pytest.mark.parametrize('max_workers', [1, None])
def test_parallel(max_workers):
    """
    Test least cost xmission and compare with baseline data
    """
    truth = os.path.join(TESTDATADIR, 'xmission', 'least_cost_100MW.csv')
    if not os.path.exists(truth):
        sc_point_gids = None
    else:
        truth = pd.read_csv(truth)
        sc_point_gids = truth['sc_point_gid'].unique()
        sc_point_gids = np.random.choice(sc_point_gids,
                                         size=N_SC_POINTS, replace=False)

    test = LeastCostXmission.run(COST_H5, FEATURES, 100,
                                 max_workers=max_workers,
                                 sc_point_gids=sc_point_gids)

    if not isinstance(truth, pd.DataFrame):
        test.to_csv(truth, index=False)
        truth = pd.read_csv(truth)

    check(truth, test)


@pytest.mark.parametrize('resolution', [64, 128])
def test_resolution(resolution):
    """
    Test least cost xmission and compare with baseline data
    """
    if resolution == 128:
        truth = os.path.join(TESTDATADIR, 'xmission',
                             'least_cost_100MW.csv')
    else:
        truth = os.path.join(TESTDATADIR, 'xmission',
                             'least_cost_100MW-64x.csv')

    if not os.path.exists(truth):
        sc_point_gids = None
    else:
        truth = pd.read_csv(truth)
        sc_point_gids = truth['sc_point_gid'].unique()
        sc_point_gids = np.random.choice(sc_point_gids,
                                         size=N_SC_POINTS, replace=False)

    test = LeastCostXmission.run(COST_H5, FEATURES, 100, resolution=resolution,
                                 sc_point_gids=sc_point_gids)

    if not isinstance(truth, pd.DataFrame):
        test.to_csv(truth, index=False)
        truth = pd.read_csv(truth)

    check(truth, test)


def test_cli(runner):
    """
    Test CostCreator CLI
    """
    truth = os.path.join(TESTDATADIR, 'xmission', 'least_cost_100MW.csv')
    truth = pd.read_csv(truth)
    sc_point_gids = truth['sc_point_gid'].unique()
    sc_point_gids = np.random.choice(sc_point_gids, size=N_SC_POINTS,
                                     replace=False)

    with tempfile.TemporaryDirectory() as td:
        config = {
            "directories": {
                "log_directory": td,
                "output_directory": td
            },
            "execution_control": {
                "option": "local",
            },
            "cost_fpath": COST_H5,
            "features_fpath": FEATURES,
            "capacity_class": '100MW',
            "dirout": td,
            "sc_point_gids": sc_point_gids
        }
        config_path = os.path.join(td, 'config.json')
        with open(config_path, 'w') as f:
            json.dump(config, f)

        result = runner.invoke(main, ['from-config',
                                      '-c', config_path])
        msg = ('Failed with error {}'
               .format(traceback.print_exception(*result.exc_info)))
        assert result.exit_code == 0, msg

        print(os.listdir(td))
        raise RuntimeError

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
