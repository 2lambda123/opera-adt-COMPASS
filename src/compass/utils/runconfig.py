from __future__ import annotations
from dataclasses import dataclass
from itertools import cycle
import os
from types import SimpleNamespace

import journal
import yamale
from ruamel.yaml import YAML

from compass.utils import helpers
from compass.utils.wrap_namespace import wrap_namespace
from sentinel1_reader.sentinel1_burst_slc import Sentinel1BurstSlc
from sentinel1_reader.sentinel1_orbit_reader import get_swath_orbit_file_from_list
from sentinel1_reader.sentinel1_reader import burst_from_zip


def validate_group_dict(group_cfg: dict) -> None:
    """Check and validate runconfig entries.

    Parameters
    ----------
    group_cfg : dict
        Dictionary storing runconfig options to validate
    """
    error_channel = journal.error('runconfig.validate_group_dict')

    # Check 'input_file_group' section of runconfig
    input_group = group_cfg['input_file_group']
    # If is_reference flag is False, check that file path to reference
    # burst is assigned and valid (required by geo2rdr and resample)
    is_reference = input_group['reference_burst']['is_reference']
    if not is_reference:
        helpers.check_file_path(input_group['reference_burst']['file_path'])

    # Check SAFE files
    run_pol_mode = group_cfg['processing']['polarization']
    for safe_file in input_group['safe_file_path']:
        # Check if files exists
        helpers.check_file_path(safe_file)

        # Check safe pol mode
        helpers.check_file_polarization_mode(safe_file, run_pol_mode)

    for orbit_file in input_group['orbit_file_path']:
        helpers.check_file_path(orbit_file)

    # Check 'dynamic_ancillary_file_groups' section of runconfig
    # Check that DEM file exists and is GDAL-compatible
    dem_path = group_cfg['dynamic_ancillary_file_group']['dem_file']
    helpers.check_file_path(dem_path)
    helpers.check_dem(dem_path)

    # Check 'product_path_group' section of runconfig.
    # Check that directories herein have writing permissions
    product_path_group = group_cfg['product_path_group']
    helpers.check_write_dir(product_path_group['product_path'])
    helpers.check_write_dir(product_path_group['scratch_path'])
    helpers.check_write_dir(product_path_group['sas_output_file'])


def load_bursts(cfg: SimpleNamespace) -> list[Sentinel1BurstSlc]:
    '''For each burst find corresponding orbit'

    Parameters
    ----------
    cfg : SimpleNamespace
        Configuration of bursts to be loaded.

    Returns
    -------
    _ : list[Sentinel1BurstSlc]
        List of bursts loaded according to given configuration.
    '''
    error_channel = journal.error('runconfig.correlate_burst_to_orbit')

    # dict to store bursts keyed by burst_ids
    bursts = {}

    # zip pol and IW subswath indices together
    mode_to_pols = {'co-pol':['VV'], 'cross-pol':['VH'], 'dual-pol':['VV', 'VH']}
    pols = mode_to_pols[cfg.processing.polarization]
    i_subswaths = [1, 2, 3]
    zip_list = zip(cycle(pols), i_subswaths)

    # extract given SAFE zips to find bursts identified in cfg.burst_id
    for safe_file in cfg.input_file_group.safe_file_path:

        # find orbit file
        orbit_path = get_swath_orbit_file_from_list(
            safe_file,
            cfg.input_file_group.orbit_file_path)

        if not orbit_path:
            err_str = f"No orbit file correlates to safe file: {os.path.basename(safe_file)}"
            error_channel.log(err_str)
            raise ValueError(err_str)

        # loop over pols and subswath index
        for pol, i_subswath in zip_list:

            # loop over burst objs extracted from SAFE zip
            for b in burst_from_zip(safe_file, orbit_path, i_subswath, pol):

                b_id = b.burst_id

                # check if b_id is wanted and if already stored
                if b_id in cfg.input_file_group.burst_id and b_id not in bursts.keys():
                    bursts[b_id] = b

    if not bursts:
        err_str = "None of given burst IDs not found in provided safe files"
        error_channel.log(err_str)
        raise ValueError(err_str)

    unaccounted_bursts = [b_id for b_id in cfg.input_file_group.burst_id
                          if b_id not in bursts]
    if unaccounted_bursts:
        err_str = f"Following burst ID(s) not found in provided safe files: {unaccounted_bursts}"
        error_channel.log(err_str)
        raise ValueError(err_str)

    return bursts.values()


@dataclass(frozen=True)
class RunConfig:
    '''dataclass containing CSLC runconfig'''
    name: str
    groups: SimpleNamespace
    bursts: list[Sentinel1BurstSlc]

    @classmethod
    def load_from_yaml(cls, yaml_path: str, workflow_name: str) -> RunConfig:
        """Initialize RunConfig class with options from given yaml file.

        Parameters
        ----------
        yaml_path : str
            Path to yaml file containing the options to load
        workflow_name: str
            Name of the workflow for which uploading default options
        """
        error_channel = journal.error('RunConfig.load_from_yaml')
        try:
            # Load schema corresponding to 'workflow_name' and to validate against
            schema = yamale.make_schema(
                f'{helpers.WORKFLOW_SCRIPTS_DIR}/schemas/cslc_s1.yaml',
                parser='ruamel')
        except:
            err_str = f'unable to load schema for workflow {workflow_name}.'
            error_channel.log(err_str)
            raise ValueError(err_str)

        # load yaml file or string from command line
        if os.path.isfile(yaml_path):
            try:
                data = yamale.make_data(yaml_path, parser='ruamel')
            except yamale.YamaleError as yamale_err:
                err_str = f'Yamale unable to load {workflow_name} runconfig yaml {yaml_path} for validation.'
                error_channel.log(err_str)
                raise yamale.YamaleError(err_str) from yamale_err
        else:
            raise FileNotFoundError

        # validate yaml file taken from command line
        try:
            yamale.validate(schema, data)
        except yamale.YamaleError as yamale_err:
            err_str = f'Validation fail for {workflow_name} runconfig yaml {yaml_path}.'
            error_channel.log(err_str)
            raise yamale.YamaleError(err_str) from yamale_err

        # load default runconfig
        parser = YAML(typ='safe')
        default_cfg_path = f'{helpers.WORKFLOW_SCRIPTS_DIR}/defaults/cslc_s1.yaml'
        with open(default_cfg_path, 'r') as f_default:
            default_cfg = parser.load(f_default)

        with open(yaml_path, 'r') as f_yaml:
            user_cfg = parser.load(f_yaml)

        # Copy user-supplied configuration options into default runconfig
        helpers.deep_update(default_cfg, user_cfg)

        # Validate YAML values under groups dict
        validate_group_dict(default_cfg['runconfig']['groups'])

        # Convert runconfig dict to SimpleNamespace
        sns = wrap_namespace(default_cfg['runconfig']['groups'])

        bursts = load_bursts(sns)

        return cls(default_cfg['runconfig']['name'], sns, bursts)

    @property
    def burst_id(self) -> list[str]:
        return self.groups.input_file_group.burst_id

    @property
    def dem(self) -> str:
        return self.groups.dynamic_ancillary_file_group.dem_file

    @property
    def is_reference(self) -> bool:
        return self.groups.input_file_group.reference_burst.is_reference

    @property
    def orbit_path(self) -> bool:
        return self.groups.input_file_group.orbit_file_path

    @property
    def polarization(self) -> list[str]:
        return self.groups.processing.polarization

    @property
    def product_path(self):
        return self.groups.product_path_group.product_path

    @property
    def reference_path(self) -> str:
        return self.groups.reference_burst.file_path

    @property
    def rdr2geo_params(self) -> dict:
        return self.groups.processing.rdr2geo

    @property
    def safe_files(self) -> list[str]:
        return self.groups.input_file_group.safe_file_path

    @property
    def sas_output_file(self):
        return self.groups.product_path_group.sas_output_file

    @property
    def scratch_path(self):
        return self.groups.product_path_group.scratch_path

    @property
    def gpu_enabled(self):
        return self.groups.worker.gpu_enabled

    @property
    def gpu_id(self):
        return self.groups.worker.gpu_id
