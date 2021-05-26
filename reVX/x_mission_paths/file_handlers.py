"""
Functions for loading substations, transmission line, etc

Mike Bannister
5/18/2021
"""
import os

import numpy as np
import geopandas as gpd
import fiona
from shapely.geometry import Point
import rasterio as rio

from .config import TEMPLATE_SHAPE, power_classes, power_to_voltage
from .utilities import RowColTransformer


class LoadData:
    """
    Load data from disk
    """
    def __init__(self, capacity, resolution,
                 costs_raster_dir='cost_rasters',
                 template_f='data/conus_template.tif',
                 landuse_f='data/nlcd.npy',
                 slope_f='data/slope.npy',
                 sc_points_f='data/sc_points/sc32_points_updated.shp',
                 t_lines_f='data/t_lines/t_lines_conus.shp',
                 subs_f='data/substations/substations_conus_updated.shp',
                 iso_regions_f='data/iso_regions.tiff'):
        """
        Parameters
        ----------
        capacity : String
            Desired reV power capacity class, one of "100MW", "200MW", "400MW",
            "1000MW"
        resolution : Int
            Desired Supply Curve Point resolution, one of: 32, 64, 128

        """
        assert capacity in power_classes.keys()

        self.capacity = capacity
        self.rct = RowColTransformer(template_f)

        # TODO - make this resolution aware
        self.sc_points = load_sc_points(sc_points_f, self.rct)
        self.subs = SubstationsLoader(subs_f)
        self.t_lines = TransLineLoader(t_lines_f)

        # Real world power capacity (MW)
        self.tie_power = power_classes[capacity]

        # Voltage (kV) corresponding to self.tie_power
        self.tie_voltage = power_to_voltage[str(self.tie_power)]

        costs_f = os.path.join(costs_raster_dir,
                               f'costs_{self.tie_power}MW.tif')
        self.costs_arr = load_raster(costs_f)


class FilterData:
    """
    Filter input data by power class
    """
    def __init__(self, ld):
        """
        Parameters
        ----------
        ld : LoadData
            LoadData instance for desired capacity and resolution
        """
        self._ld = ld

        self.subs = ld.subs.filter(ld.tie_voltage)
        self.t_lines = ld.t_lines.filter(ld.tie_voltage)


class SubstationsLoader:
    def __init__(self, substations_f):
        """
        Load substations from disc

        Parameters
        ----------
        substations_f : String
            Path to substations shapefile
        """
        subs = gpd.read_file(substations_f)
        subs = subs[subs.Proposed == "In Service"]
        self.subs = subs.drop(['Owner', 'Tap', 'Proposed', 'County', 'State',
                               'Location_C', 'Source', 'Owner2', 'Notes',
                               'row', 'column', 'Entity_ID', 'Owner_ID',
                               'Owner2_ID', 'Layer_ID', 'Rec_ID'], axis=1)

    def filter(self, cutoff_voltage):
        """
        Filter substations by minimum max voltage

        Parameters
        ----------
        cutoff_voltage : Int
            Minimum voltage substations to include (kV)

        Returns
        -------
        subs : Geopandas.DataFrame
        """
        # TODO - move all cutoff_voltage to FilterData
        subs = self.subs[self.subs.Max_Voltag >= cutoff_voltage]
        return subs


class TransLineLoader:
    def __init__(self, t_lines_f):
        """
        Load transmission lines from disk. Drop all proposed lines

        Parameters
        ----------
        t_lines_f : String
            Path to transmission lines shapefile
        """
        tls = gpd.read_file(t_lines_f)
        tls = tls[tls.Proposed == "In Service"]
        self.tls = tls.drop(['Owner2', 'Number_of_', 'Proposed', 'Undergroun',
                             'From_Sub', 'To_Sub', 'Notes', 'Length_mi',
                             'Location_C', 'Source', 'Numeric_Vo',
                             'Holding_Co', 'Company_ID', 'Owner2_ID',
                             'Entity_ID', 'Holding__1', 'Rec_ID', 'Layer_ID',
                             'Type', 'Owner_Type'], axis=1)

    def filter(self, cutoff_voltage):
        """
        Filter trans lines by minimum voltage

        Parameters
        ----------
        cutoff_voltage : Int
            Minimum voltage transmission lines to include (kV)

        Returns
        -------
        tls : Geopandas.DataFrame
        """
        tls = self.tls[self.tls.Voltage_kV >= cutoff_voltage]
        return tls


class SupplyCurvePoint:
    def __init__(self, id, x, y, rct):
        """
        Represents a supply curve point for possible renewable energy plant.

        Parameters
        ----------
        id : int
            Id of supply curve point
        x : float
            Projected easting coordinate
        y : float
            Projected northing coordinate
        rct : RowColTransformer
            Transformer for template raster
        """
        self.id = id
        self.x = x
        self.y = y

        # Calculate and save location on template raster
        row, col = rct.get_row_col(x, y)
        self.row = row
        self.col = col

    @property
    def point(self):
        """
        Return point as shapley.geometry.Point object

        """
        return Point(self.x, self.y)

    def __repr__(self):
        return f'id={self.id}, coords=({self.x}, {self.y}), ' +\
               f'r/c=({self.row}, {self.col})'


def load_sc_points(sc_points_f, rct):
    """
    Load supply curve points from disk

    Parameters
    ----------
    sc_points_f : String
        Path to supply curve points
    rct : RowColTransformer
        Transformer for template raster

    Returns
    -------
    sc_points : List of SupplyCurvePoint
    """
    sc_points = []
    with fiona.open(sc_points_f) as src:
        for feat in src:
            sc_pt = SupplyCurvePoint(feat['properties']['sc_gid'],
                                     feat['geometry']['coordinates'][0],
                                     feat['geometry']['coordinates'][1],
                                     rct)
            sc_points.append(sc_pt)
    return sc_points


def load_raster(f_name):
    """
    Load raster in same shape as template from disc.

    Parameters
    ----------
    f_name : String
        Path and name of raster

    Returns
    -------
    data : numpy.ndarray
    """
    _, ext = os.path.splitext(f_name)

    if ext == '.tif' or ext == '.tiff':
        with rio.open(f_name) as dataset:
            data = dataset.read(1)

    elif ext == '.npy':
        data = np.load(f_name)
        if len(data.shape) == 3:
            data = data[0]

    else:
        raise ValueError(f'Unknown extension type on {f_name}')

    assert data.shape == TEMPLATE_SHAPE
    return data
