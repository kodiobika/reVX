# -*- coding: utf-8 -*-
"""
RED-E reV based tech potential tool
"""
import logging
from reV.supply_curve.exclusions import ExclusionMaskFromDict

logger = logging.getLogger(__name__)


class TechPotential(ExclusionMaskFromDict):
    """
    RED-E Tech Potential tool
    """
    def __init__(self, h5_path, base_layer, layer_dict, power_density=1,
                 hsds=False, min_area=None, kernel='queen'):
        """
        Parameters
        ----------
        h5_path : str
            Path to .h5 file containing CF means and exclusion layers
        base_layer : str
            Name of dataset in .h5 file containing base layer
        layers_dict : dcit
            Dictionary of LayerMask arugments {layer: {kwarg: value}}
        power_density : float
            Multiplier to convert CF means to generation means
        hsds : bool
            Boolean flag to use h5pyd to handle .h5 'files' hosted on AWS
            behind HSDS
        min_area : float | NoneType
            Minimum required contiguous area in sq-km
        kernel : str
            Contiguous filter method to use on final exclusion
        """
        excl_dict = layer_dict.copy()
        excl_dict[base_layer] = {"use_as_weights": True}
        super().__init__(h5_path, excl_dict, hsds=hsds, min_area=min_area,
                         kernel=kernel)
        self._pd = power_density

    @property
    def profile(self):
        """
        GeoTiff profile for exclusions

        Returns
        -------
        profile : dict
            Generic GeoTiff profile for exclusions in .h5 file
        """
        return self.excl_h5.profile

    @property
    def generation(self):
        """
        Tech-potential as generation

        Returns
        -------
        gen : ndarray
        """
        gen = self[...]
        return gen * self._pd

    @classmethod
    def run(cls, h5_path, base_layer, layer_dict, power_density=1,
            hsds=False, min_area=None, kernel='queen'):
        """
        compute tech-potential

        Parameters
        ----------
        h5_path : str
            Path to .h5 file containing CF means and exclusion layers
        base_layer : str
            Name of dataset in .h5 file containing base layer
        layers_dict : dcit
            Dictionary of LayerMask arugments {layer: {kwarg: value}}
        power_density : float
            Multiplier to convert CF means to generation means
        hsds : bool
            Boolean flag to use h5pyd to handle .h5 'files' hosted on AWS
            behind HSDS
        min_area : float | NoneType
            Minimum required contiguous area in sq-km
        kernel : str
            Contiguous filter method to use on final exclusion

        Returns
        -------
        mask : ndarray
            Base layer with exclusions masked out
        """
        with cls(h5_path, base_layer, layer_dict, power_density=power_density,
                 hsds=hsds, min_area=min_area, kernel=kernel) as f:
            mask = f.mask

        return mask

    @classmethod
    def run_generation(cls, h5_path, base_layer, layer_dict, power_density=1,
                       hsds=False, min_area=None, kernel='queen'):
        """
        compute tech-potential

        Parameters
        ----------
        h5_path : str
            Path to .h5 file containing CF means and exclusion layers
        base_layer : str
            Name of dataset in .h5 file containing base layer
        layers_dict : dcit
            Dictionary of LayerMask arugments {layer: {kwarg: value}}
        power_density : float
            Multiplier to convert CF means to generation means
        hsds : bool
            Boolean flag to use h5pyd to handle .h5 'files' hosted on AWS
            behind HSDS
        min_area : float | NoneType
            Minimum required contiguous area in sq-km
        kernel : str
            Contiguous filter method to use on final exclusion

        Returns
        -------
        gen : ndarray
            Tech-potentail as generation
        """
        with cls(h5_path, base_layer, layer_dict, power_density=power_density,
                 hsds=hsds, min_area=min_area, kernel=kernel) as f:
            gen = f.generation

        return gen
