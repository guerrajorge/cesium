#!/usr/bin/python

from __future__ import print_function
from parse import parse
import sys
import os
try:
    import cPickle as pickle
except:
    import pickle
import uuid
import io
import tarfile
import shutil
import numpy as np
from importlib import import_module
from . import cfg
from . import celery_task_tools as ctt

from .util import docker_images_available, is_running_in_docker, \
    get_docker_client

class MissingRequiredParameterError(Exception):

    """Required parameter is not provided in feature function call."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)


class MissingRequiredReturnKeyError(Exception):

    """Required return value is not provided in feature definition."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)


class myFeature(object):

    """Decorator for custom-defined time series feature(s) function.

    Applies function wrapper that ensures required parameters and
    return values are present before executing, raising an exception if
    not.

    Attributes
    ----------
    requires : list
        List of names of features required for decorated function to
        execute.
    provides : list
        List of names of features generated by decorated function.

    """

    def __init__(self, requires, provides):
        """Instantiates object, sets args as attributes.

        Parameters
        ----------
        requires : list
            List of variable names required by the function.
        provides : list
            List of the key names of the returned dictionary - the
            features calculated by a particular function.

        """
        self.requires = requires
        self.provides = provides

    def __call__(self, f):
        """Wrap decorated function.

        Wrap decorated function with a check to ensure that required
        parameters (specified in decorator expression) are provided
        upon function call (raises MissingRequiredParameterError if
        not) and that all features reportedly returned (specified in
        decorator expression) are in fact returned (raises
        MissingRequiredReturnKeyError if not).

        Returns
        -------
        function
            The wrapped function.

        """
        def wrapped_f(*args, **kwargs):
            for required_arg in self.requires:
                if required_arg not in args and required_arg not in kwargs:
                    raise MissingRequiredParameterError(
                        "Required arg %s not provided in function call." %
                        required_arg)
            result_dict = f(*args, **kwargs)
            for provided in self.provides:
                if provided not in result_dict:
                    raise MissingRequiredReturnKeyError(
                        "Key %s not present in function return value." %
                        provided)
            return result_dict
        return wrapped_f


class DummyFile(object):

    """Used as a file object to temporarily redirect/suppress output."""

    def write(self, x):
        pass


def parse_for_req_prov_params(script_fpath):
    """
    """
    with open(script_fpath, "r") as f:
        all_lines = f.readlines()
    fnames_req_prov_dict = {}
    all_required_params = []
    all_provided_params = []
    for i in range(len(all_lines) - 1):
        if "@myFeature" in all_lines[i] and "def " in all_lines[i + 1]:
            reqs_provs_1 = parse(
                "@myFeature(requires={requires}, provides={provides})",
                all_lines[i].strip())
            func_name = parse(
                "def {funcname}({args}):", all_lines[i + 1].strip())
            fnames_req_prov_dict[func_name.named['funcname']] = {
                "requires": eval(reqs_provs_1.named["requires"]),
                "provides": eval(reqs_provs_1.named["provides"])}
            all_required_params = list(set(
                all_required_params +
                list(set(eval(reqs_provs_1.named["requires"])))))
            all_provided_params = list(set(
                all_provided_params +
                list(set(eval(reqs_provs_1.named["provides"])))))
    return (fnames_req_prov_dict, all_required_params, all_provided_params)


def call_custom_functions(features_already_known, all_required_params,
                          all_provided_params, fnames_req_prov_dict,
                          script_fpath=None):
    """
    """
    # import the custom feature defs
    try:
        from .custom_feature_scripts import custom_feature_defs
    except ImportError:
        try:
            import custom_feature_defs
        except ImportError:
            if script_fpath:
                script_name = str(uuid.uuid4())[:10] + ".py"
                custom_feature_scripts_dir = os.path.join(
                    os.path.dirname(__file__), "custom_feature_scripts")
                copied_path = os.path.join(custom_feature_scripts_dir,
                                           script_name)
                shutil.copy(script_fpath, copied_path)
                custom_feature_defs = import_module(".custom_feature_scripts." +
                                                    script_name.replace(".py", ""),
                                                    "mltsp")

    # temporarily redirect stdout:
    save_stdout = sys.stdout
    sys.stdout = DummyFile()

    all_required_params_copy = [x for x in all_required_params
                                if x not in features_already_known]
    for reqd_param in all_required_params_copy:
        if reqd_param not in all_provided_params:
            raise Exception((
                "Not all of the required parameters are provided by the "
                "functions in this script (required parameter '%s').") %
                str(reqd_param))
    funcs_round_1 = []
    func_queue = []
    funcnames = list(fnames_req_prov_dict.keys())
    i = 0
    func_rounds = {}
    all_extracted_features = {}
    while len(funcnames) > 0:
        func_rounds[str(i)] = []
        for funcname in funcnames:
            reqs_provs_dict = fnames_req_prov_dict[funcname]
            reqs = reqs_provs_dict['requires']
            provs = reqs_provs_dict['provides']
            if len(set(all_required_params_copy) & set(reqs)) > 0:
                func_queue.append(funcname)
            else:
                func_rounds[str(i)].append(funcname)
                all_required_params_copy = [x for x in all_required_params_copy
                                            if x not in provs]
                arguments = {}
                for req in reqs:
                    if req in features_already_known:
                        arguments[req] = features_already_known[req]
                    elif req in all_extracted_features:
                        arguments[req] = all_extracted_features[req]
                func_result = getattr(
                    custom_feature_defs, funcname)(**arguments)
                all_extracted_features = dict(
                    list(all_extracted_features.items()) +
                    list(func_result.items()))
                funcnames.remove(funcname)
        i += 1
    # revert to original stdout
    sys.stdout = save_stdout
    try:
        os.remove(copied_path)
        os.remove(copied_path.replace('.py', '.pyc'))
    except:
        pass
    return all_extracted_features


def execute_functions_in_order(
        script_fpath,
        features_already_known={
            "t": [1, 2, 3], "m": [1, 23, 2], "e": [0.2, 0.3, 0.2],
            "coords": [22, 33]},
        multiple_sources=False):
    """Generate custom features defined in script_fpath.

    Parses the script (which must have function definitions with
    decorators specifying the required parameters and those which are
    provided by each function) and executes the functions defined in
    that script such that all functions whose outputs are required
    as inputs of other functions are called first, if possible,
    otherwise raises an Exception.

    Parameters
    ----------
    script_fpath : str
        Path to custom feature definitions script.
    features_already_known : dict
        Dictionary providing all time-series data (time ("t"), magnitude
        ("m"), error ("e") as keys) and any meta-features.
        Example:
            {"t": [1, 2, 3], "m": [10.32, 11.41, 11.06],
             "e": [0.2015,0.3134,0.2953], "coords": [22.55,33.01]}

    Returns
    -------
    dict
        Dictionary of all extracted features (key-value pairs are
        feature name and feature value respectively).

    """
    # For when run inside Docker container:
    try:
        sys, os
    except NameError:
        import sys
        import os

    fnames_req_prov_dict, all_required_params, all_provided_params = \
        parse_for_req_prov_params(script_fpath)

    all_extracted_features = call_custom_functions(
        features_already_known, all_required_params, all_required_params,
        fnames_req_prov_dict, script_fpath)

    return all_extracted_features


def make_tmp_dir():
    """
    """
    if os.path.exists(cfg.PROJECT_PATH_LINK):
        proj_path = cfg.PROJECT_PATH_LINK
    else:
        proj_path = cfg.PROJECT_PATH
    path_to_tmp_dir = os.path.join(proj_path, "tmp",
                                   str(uuid.uuid4())[:10])
    os.makedirs(path_to_tmp_dir)
    return path_to_tmp_dir


def generate_random_str():
    """Generate random 10-character string using uuid.uuid4.
    """
    return str(uuid.uuid4())[:10]


def copy_data_to_tmp_dir(path_to_tmp_dir, script_fpath,
                         features_already_known):
    """
    """
    shutil.copy(script_fpath,
                os.path.join(path_to_tmp_dir, "custom_feature_defs.py"))
    with open(os.path.join(path_to_tmp_dir, "features_already_known.pkl"),
              "wb") as f:
        pickle.dump(features_already_known, f, protocol=2)
    # Create __init__.py file so that custom feats script can be imported
    open(os.path.join(path_to_tmp_dir, "__init__.py"), "w").close()
    return


def docker_copy(docker_client, container_id, path, target="."):
    """Copy file from docker container to host machine.

    Parameters
    ----------
    docker_client : docker.Client object
        The connected Docker client.
    container_id : str
        ID of the container to copy from.
    path : str
        Path to the file in the container.
    target : str
        Folder where to put the file.

    """
    response = docker_client.copy(container_id, path)
    buffer = io.BytesIO()
    buffer.write(response.data)
    buffer.seek(0)
    tar = tarfile.open(fileobj=buffer, mode='r|')
    tar.extractall(path=target)


def extract_feats_in_docker_container(container_name, path_to_tmp_dir):
    """
    """
    tmp_data_dir = path_to_tmp_dir
    try:
        # Spin up Docker contain and extract custom feats
        # Instantiate Docker client
        client = get_docker_client()

        # Use symlink if one was created (in which case this is probably
        # being run in a Disco worker)
        if os.path.exists(cfg.PROJECT_PATH_LINK):
            proj_mount_path = cfg.PROJECT_PATH_LINK
        else:
            proj_mount_path = cfg.PROJECT_PATH
        # Create container
        cont_id = client.create_container(
            image="mltsp/base",
            command="python {}/run_script_in_container.py --{} --tmp_dir={}".format(
                proj_mount_path, "extract_custom_feats", tmp_data_dir),
            tty=True,
            volumes="{}:{}".format("", proj_mount_path))["Id"]

        # Start container
        client.start(cont_id,
                     binds={proj_mount_path: {"bind": proj_mount_path,
                                              "ro": True}})
        # Wait for process to complete
        client.wait(cont_id)
        stdout = client.logs(container=cont_id, stdout=True)
        stderr = client.logs(container=cont_id, stderr=True)
        if str(stderr).strip() != "" and stderr != b'':
            print("\n\ndocker container stderr:\n\n", str(stderr).strip(), "\n\n")
        # Copy pickled results data from Docker container to host
        docker_copy(client, cont_id, "/tmp/results_dict.pkl",
                    target=path_to_tmp_dir)
        print("/tmp/results_dict.pkl copied to host machine.")
        # Load pickled results data
        with open(os.path.join(path_to_tmp_dir, "results_dict.pkl"),
                  "rb") as f:
            return pickle.load(f)
    except:
        raise
    finally:
        # Kill and remove the container
        try:
            client.remove_container(container=cont_id, force=True)
        except UnboundLocalError:
            print("Error occurred in running Docker container.")


def remove_tmp_files(path_to_tmp_dir):
    """
    """
    # Remove tmp dir
    shutil.rmtree(path_to_tmp_dir, ignore_errors=True)
    for tmp_file in (os.path.join(cfg.TMP_CUSTOM_FEATS_FOLDER,
                                  "custom_feature_defs.py"),
                     os.path.join(cfg.TMP_CUSTOM_FEATS_FOLDER,
                                  "custom_feature_defs.pyc"),
                     os.path.join(cfg.TMP_CUSTOM_FEATS_FOLDER,
                                  "__init__.pyc")):
        try:
            os.remove(tmp_file)
        except OSError:
            pass
    return


def docker_extract_features(script_fpath, features_already_known):
    """Extract custom features in a Docker container.

    Spins up a docker container in which custom script
    excecution/feature extraction is done inside. Resulting data are
    copied to host machine and returned as a dict.

    Parameters
    ----------
    script_fpath : str
        Path to script containing custom feature definitions.
    features_already_known : dict
        List of dictionaries containing time series data (t,m,e) and
        any meta-features to be used in generating custom features.
        Defaults to []. NOTE: If omitted, or if "t" or "m" are not
        among contained dict keys, (a) respective element of
        `ts_datafile_paths` or (b) `ts_data` (see below) MUST not
        be None, otherwise raises ValueError.
    ts_data: list of list OR str, optional
        List of either (a) list of lists/tuples each containing t,m(,e)
        for each epoch, or (b) string containing equivalent comma-
        separated lines, each line being separated by a newline
        character ("\n"). Defaults to None. NOTE: If None, either
        `ts_datafile_paths` must not be None or "t" (time) and "m"
        (magnitude/measurement) must be among the keys of
        respective element of `features_already_known` (see
        above), otherwise raisesValueError.

    Returns
    -------
    list of dict
        List of dictionaries of all generated features.

    """
    container_name = generate_random_str()
    path_to_tmp_dir = make_tmp_dir()

    copy_data_to_tmp_dir(path_to_tmp_dir, script_fpath, features_already_known)

    try:
        results_dict = extract_feats_in_docker_container(
            container_name, path_to_tmp_dir)
    except:
        raise
    finally:
        remove_tmp_files(path_to_tmp_dir)
    return results_dict


def assemble_test_data():
    """
    """
    fname = os.path.join(cfg.SAMPLE_DATA_PATH, "dotastro_215153.dat")
    t, m, e = ctt.parse_ts_data(fname)
    features_already_known = {'t': t, 'm': m, 'e': e}
    return features_already_known


def verify_new_script(script_fpath, docker_container=False):
    """Test custom features script and return generated features.

    Performs test run on custom feature def script with trial time
    series data sets and returns list of dicts containing extracted
    features if successful, otherwise raises an exception.

    Parameters
    ----------
    script_fpath : str
        Path to custom feature definitions script.
    docker_container : bool, optional
        Boolean indicating whether function is being called from within
        a Docker container.

    Returns
    -------
    list of dict
        List of dictionaries of extracted features for each of the trial
        time-series data sets.

    """
    features_already_known = assemble_test_data()
    print(script_fpath, os.path.isfile(script_fpath))

    all_extracted_features = {}
    no_docker = (os.getenv("MLTSP_NO_DOCKER") == "1")
    if docker_images_available() and not no_docker:
        print("Extracting features inside docker container...")
        all_extracted_features = docker_extract_features(
            script_fpath=script_fpath,
            features_already_known=features_already_known)
    elif no_docker:
        print("WARNING - generating custom features WITHOUT docker container...")
        all_extracted_features = execute_functions_in_order(
            features_already_known=features_already_known,
            script_fpath=script_fpath)
    elif not docker_images_available():
        raise Exception("Docker image not available.")
    return all_extracted_features


def list_features_provided(script_fpath):
    """Parses script and returns a list of all features it provides.

    Parses decorator expression in custom feature definitions script,
    returning a list of all feature names generated by the various
    definitions in that script.

    Parameters
    ----------
    script_fpath : str
        Path to custom features definition script.

    Returns
    -------
    list of str
        List of feature names that the script will generate.

    """
    with open(script_fpath, "r") as f:
        all_lines = f.readlines()
    fnames_req_prov_dict = {}
    all_required_params = []
    all_provided_params = []
    for i in range(len(all_lines) - 1):
        if "@myFeature" in all_lines[i] and "def " in all_lines[i + 1]:
            reqs_provs_1 = parse(
                "@myFeature(requires={requires}, provides={provides})",
                all_lines[i].strip())
            func_name = parse(
                "def {funcname}({args}):", all_lines[i + 1].strip())
            fnames_req_prov_dict[func_name.named['funcname']] = {
                "requires": eval(reqs_provs_1.named["requires"]),
                "provides": eval(reqs_provs_1.named["provides"])}
            all_required_params = list(set(
                all_required_params +
                list(set(eval(reqs_provs_1.named["requires"])))))
            all_provided_params = list(set(
                all_provided_params +
                list(set(eval(reqs_provs_1.named["provides"])))))
    return all_provided_params


def generate_custom_features(custom_script_path, t, m, e,
                             features_already_known={}):
    """Generate custom features for provided TS data and script.

    Parameters
    ----------
    t : array_like
        Array containing time values.

    m : array_like
        Array containing data values.

    e : array_like
        Array containing measurement error values.

    custom_script_path : str
        Path to custom features script.

    features_already_known : dict, optional
        Dict containing any meta-features associated with provided time-series
        data. Defaults to {}.

    Returns
    -------
    dict
        Dictionary containing newly-generated features.
    """
    if "t" not in features_already_known:
        features_already_known['t'] = t
    if "m" not in features_already_known:
        features_already_known['m'] = m
    if e is not None and len(e) == len(m) and "e" not in features_already_known:
        features_already_known['e'] = e
    for k in ('t', 'm', 'e'):
        if k in features_already_known:
            features_already_known[k] = np.array(features_already_known[k])

    if is_running_in_docker():
        all_new_features = execute_functions_in_order(
            features_already_known=features_already_known,
            script_fpath=custom_script_path)
    else:
        no_docker = (os.getenv("MLTSP_NO_DOCKER") == "1")
        if docker_images_available() and not no_docker:
            print("Generating custom features inside docker container...")
            all_new_features = docker_extract_features(
                script_fpath=custom_script_path,
                features_already_known=features_already_known)
        elif no_docker:
            print("WARNING - generating custom features WITHOUT docker container...")
            all_new_features = execute_functions_in_order(
                features_already_known=features_already_known,
                script_fpath=custom_script_path)
        elif not docker_images_available():
            raise Exception("Docker image not available.")

    return all_new_features
