# -*- coding: utf-8 -*-
"""
pytests for resource extractors
"""
import numpy as np
import os
import pandas as pd
import pytest
from reVX.resource.resource import NSRDBX, WindX
from reVX import TESTDATADIR


@pytest.fixture
def NSRDBX_cls():
    """
    Init NSRDB resource handler
    """
    path = os.path.join(TESTDATADIR, 'nsrdb/ri_100_nsrdb_2012.h5')
    return NSRDBX(path)


@pytest.fixture
def WindX_cls():
    """
    Init WindResource resource handler
    """
    path = os.path.join(TESTDATADIR, 'wtk/ri_100_wtk_2012.h5')
    return WindX(path)


def check_props(res_cls):
    """
    Test extraction class properties
    """
    meta = res_cls['meta']

    assert np.all(np.in1d(res_cls.countries, meta['country'].unique()))
    assert np.all(np.in1d(res_cls.states, meta['state'].unique()))
    assert np.all(np.in1d(res_cls.counties, meta['county'].unique()))


def extract_site(res_cls, ds_name):
    """
    Run tests extracting a single site
    """
    time_index = res_cls['time_index']
    meta = res_cls['meta']
    site = np.random.choice(len(meta), 1)[0]
    lat_lon = meta.loc[site, ['latitude', 'longitude']].values
    truth_ts = res_cls[ds_name, :, site]
    truth_df = pd.DataFrame({ds_name: truth_ts}, index=time_index)

    site_ts = res_cls.get_site_ts(ds_name, lat_lon)
    assert np.allclose(truth_ts, site_ts)

    site_df = res_cls.get_site_df(ds_name, lat_lon)
    assert site_df.equals(truth_df)


def extract_region(res_cls, ds_name, region, region_col='county'):
    """
    Run tests extracting a single site
    """
    time_index = res_cls['time_index']
    meta = res_cls['meta']
    sites = (meta[region_col] == region).index.values
    truth_ts = res_cls[ds_name, :, sites]
    truth_df = pd.DataFrame(columns=sites, index=time_index)
    truth_df.loc[:, :] = truth_ts

    lat_lon = meta.loc[sites, ['latitude', 'longitude']].values
    region_ts = res_cls.get_site_ts(ds_name, lat_lon)
    assert np.allclose(truth_ts, region_ts)

    region_df = res_cls.get_site_df(ds_name, lat_lon)
    assert region_df.equals(truth_df)

    region_ts = res_cls.get_region_ts(ds_name, region, region_col=region_col)
    assert np.allclose(truth_ts, region_ts)

    region_df = res_cls.get_region_df(ds_name, region, region_col=region_col)
    assert region_df.equals(truth_df)


class TestNSRDBX:
    """
    NSRDBX Resource Extractor
    """
    @staticmethod
    def test_props(NSRDBX_cls):
        """
        test NSRDBX properties
        """
        check_props(NSRDBX_cls)
        NSRDBX_cls.close()

    @staticmethod
    def test_site(NSRDBX_cls, ds_name='dni'):
        """
        test site data extraction
        """
        extract_site(NSRDBX_cls, ds_name)
        NSRDBX_cls.close()

    @staticmethod
    def test_region(NSRDBX_cls, ds_name='ghi', region='Washington',
                    region_col='county'):
        """
        test region data extraction
        """
        extract_region(NSRDBX_cls, ds_name, region, region_col=region_col)
        NSRDBX_cls.close()


class TestWindX:
    """
    WindX Resource Extractor
    """
    @staticmethod
    def test_props(WindX_cls):
        """
        test WindX properties
        """
        check_props(WindX_cls)
        WindX_cls.close()

    @staticmethod
    def test_site(WindX_cls, ds_name='windspeed_100m'):
        """
        test site data extraction
        """
        extract_site(WindX_cls, ds_name)
        WindX_cls.close()

    @staticmethod
    def test_region(WindX_cls, ds_name='windspeed_50m', region='Providence',
                    region_col='county'):
        """
        test region data extraction
        """
        extract_region(WindX_cls, ds_name, region, region_col=region_col)
        WindX_cls.close()
