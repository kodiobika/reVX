# -*- coding: utf-8 -*-
"""reVX RPM unit test module
"""
from click.testing import CliRunner
import json
import numpy as np
import os
import pytest
import pandas as pd
import tempfile
import traceback

from rex.utilities.utilities import check_tz

from reVX import TESTDATADIR
from reVX.rpm.rpm_manager import RPMClusterManager
from reVX.rpm.rpm_cli import main

JOB_TAG = 'pytest'
CF_FPATH = os.path.join(TESTDATADIR, 'reV_gen',
                        'gen_ri_pv_2012_x000.h5')
EXCL_FPATH = os.path.join(TESTDATADIR, 'ri_exclusions',
                          'ri_exclusions.h5')
TECHMAP_DSET = 'techmap_nsrdb_ri_truth'
EXCL_DICT = {'ri_srtm_slope': {'inclusion_range': (None, 5),
                               'exclude_nodata': True},
             'ri_padus': {'exclude_values': [1],
                          'exclude_nodata': True}}
RPM_META = os.path.join(TESTDATADIR, 'rpm/rpm_meta.csv')

BASELINE_CLUSTERS = os.path.join(TESTDATADIR,
                                 'rpm/rpm_cluster_outputs_baseline.csv')
BASELINE_PROFILES = os.path.join(TESTDATADIR,
                                 'rpm/rpm_rep_profiles_baseline.csv')


@pytest.fixture(scope="module")
def runner():
    """
    cli runner
    """
    return CliRunner()


def compute_centers(clusters):
    """
    Compute centers of each cluster

    Parameters
    ----------
    clusters : pandas.DataFrame
        RPM Clusters DataFrame

    Returns
    -------
    centers : ndarray
        n x 2 array of (lat, lon) centers
    """
    centers = clusters.groupby('cluster_id')[['latitude', 'longitude']].mean()

    return centers.values


def check_clusters(baseline, test):
    """
    Compare clusters by computing and comparing their centers

    Parameters
    ----------
    baseline : str
        Path to baseline clusters .csv
    test : str
        Path to test clusters .csv
    """
    baseline = compute_centers(pd.read_csv(baseline))
    test = compute_centers(pd.read_csv(test))

    assert np.allclose(baseline, test, rtol=0.001)


def load_profiles(profiles):
    """
    Load profiles from .csv

    Parameters
    ----------
    profiles : str
        path to .csv

    Returns
    -------
    profiles : pd.DataFrame
    """
    profiles = pd.read_csv(profiles)
    if 'time_index' in profiles:
        profiles = profiles.set_index('time_index')
        profiles.index = check_tz(pd.to_datetime(profiles.index))

    return profiles


def check_profiles(baseline, test):
    """
    Compare representative profiles

    Parameters
    ----------
    baseline : str
        Path to baseline representative profiles .csv
    test : str
        Path to test representative profiles .csv
    """
    baseline = load_profiles(baseline)
    test = load_profiles(test)

    assert baseline.index.equals(test.index)
    np.allclose(baseline.values, test.values, rtol=0.001)


@pytest.mark.parametrize('max_workers', [None, 1])
def test_rpm(max_workers):
    """Test the rpm clustering pipeline and run a baseline validation."""
    with tempfile.TemporaryDirectory() as td:
        RPMClusterManager.run_clusters_and_profiles(
            CF_FPATH, RPM_META, EXCL_FPATH, EXCL_DICT, TECHMAP_DSET, td,
            job_tag=JOB_TAG,
            rpm_region_col=None,
            max_workers=max_workers,
            output_kwargs=None,
            dist_rank_filter=True,
            contiguous_filter=False)

        TEST_CLUSTERS = os.path.join(td, 'rpm_cluster_outputs_{}.csv'
                                         .format(JOB_TAG))
        TEST_PROFILES = os.path.join(td, 'rpm_rep_profiles_{}_rank0.csv'
                                         .format(JOB_TAG))

        check_clusters(BASELINE_CLUSTERS, TEST_CLUSTERS)
        check_profiles(BASELINE_PROFILES, TEST_PROFILES)


def test_rpm_no_exclusions():
    """Test that the clustering works without exclusions."""
    with tempfile.TemporaryDirectory() as td:
        RPMClusterManager.run_clusters_and_profiles(
            CF_FPATH, RPM_META, None, None, TECHMAP_DSET, td,
            job_tag=JOB_TAG,
            rpm_region_col=None,
            max_workers=1,
            output_kwargs=None,
            dist_rank_filter=True,
            contiguous_filter=False)

        TEST_CLUSTERS = os.path.join(td, 'rpm_cluster_outputs_{}.csv'
                                         .format(JOB_TAG))
        TEST_PROFILES = os.path.join(td, 'rpm_rep_profiles_{}_rank0.csv'
                                         .format(JOB_TAG))

        check_clusters(BASELINE_CLUSTERS, TEST_CLUSTERS)
        baseline_profiles = os.path.join(
            TESTDATADIR, 'rpm', 'rpm_rep_profiles_baseline_noexcl.csv')
        check_profiles(baseline_profiles, TEST_PROFILES)


def test_cli(runner):
    """
    Test CLI
    """
    with tempfile.TemporaryDirectory() as td:
        config = {
            "name": JOB_TAG,
            "directories": {
                "log_directory": os.path.join(td, 'logs'),
                "output_directory": td
            },
            "execution_control": {
                "option": "local"
            },
            "cf_profiles": CF_FPATH,
            "cluster": {
                "rpm_meta": RPM_META,
                "contiguous_filter": False,
            },
            "rep_profiles": {
                "rpm_clusters": None,
                "exclusions": EXCL_FPATH,
                "excl_dict": EXCL_DICT,
                "techmap_dset": TECHMAP_DSET
            }
        }
        config_path = os.path.join(td, 'config.json')
        with open(config_path, 'w') as f:
            json.dump(config, f)

        result = runner.invoke(main, ['from-config',
                                      '-c', config_path])
        msg = ('Failed with error {}'
               .format(traceback.print_exception(*result.exc_info)))
        assert result.exit_code == 0, msg

        TEST_CLUSTERS = os.path.join(td, 'rpm_cluster_outputs_{}.csv'
                                         .format(JOB_TAG))
        TEST_PROFILES = os.path.join(td, 'rpm_rep_profiles_{}_rank0.csv'
                                         .format(JOB_TAG))

        check_clusters(BASELINE_CLUSTERS, TEST_CLUSTERS)
        check_profiles(BASELINE_PROFILES, TEST_PROFILES)


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
