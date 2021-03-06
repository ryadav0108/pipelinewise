import os
import sys
import json
import yaml
import errno
import glob
import shlex
import copy
import re
import logging
import jsonschema

from subprocess import Popen, PIPE, STDOUT
from datetime import date, datetime
from collections import MutableMapping
from contextlib import suppress

from ansible.parsing.vault import VaultLib, get_file_vault_secret, is_encrypted_file
from ansible.parsing.yaml.loader import AnsibleLoader
from ansible.parsing.dataloader import DataLoader
from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode
from ansible.utils.unsafe_proxy import AnsibleUnsafe
from ansible.module_utils._text import to_text
from ansible.module_utils.common._collections_compat import Mapping
from ansible.errors import AnsibleError

from . import tap_properties

logger = logging.getLogger('Pipelinewise CLI')


class AnsibleJSONEncoder(json.JSONEncoder):
    '''
    Simple encoder class to deal with JSON encoding of Ansible internal types
    
    This is required to convert YAML files with vault encrypted inline values to
    singer JSON configuration files
    '''
    def default(self, o):
        if isinstance(o, AnsibleVaultEncryptedUnicode):
            # vault object - serialise the decrypted value as a string
            value = str(o)
        elif isinstance(o, Mapping):
            # hostvars and other objects
            value = dict(o)
        elif isinstance(o, (date, datetime)):
            # date object
            value = o.isoformat()
        else:
            # use default encoder
            value = super(AnsibleJSONEncoder, self).default(o)
        return value


class RunCommandException(Exception):
    '''
    '''
    def __init__(self, *args, **kwargs):
        Exception.__init__(self, *args, **kwargs)


def is_json(string):
    '''
    Detects if a string is a valid json or not
    '''
    try:
        json_object = json.loads(string)
    except Exception as exc:
        return False
    return True


def is_json_file(path):
    '''
    Detects if a file is a valid json file or not
    '''
    try:
        if os.path.isfile(path):
            with open(path) as f:
                if json.load(f):
                    return True
        return False
    except Exception as exc:
        return False


def load_json(path):
    '''
    Deserialise JSON file to python object
    '''
    try:
        logger.debug(f'Parsing file at {path}')
        if os.path.isfile(path):
            with open(path) as f:
                return json.load(f)
        else:
            logger.debug(f"No file at {path}")
            return None
    except Exception as exc:
        raise Exception(f"Error parsing {path} {exc}")


def is_state_message(line: str) -> bool:
    try:
        json_object = json.loads(line)
        return 'bookmarks' in json_object
    except Exception as exc:
        return False


def save_json(data, path):
    '''
    Serializes and saves any data structure to JSON files 
    '''
    try:
        logger.debug(f"Saving JSON {path}")
        with open(path, 'w') as f:
            return json.dump(data, f, cls=AnsibleJSONEncoder, indent=4, sort_keys=True)
    except Exception as exc:
        raise Exception(f"Cannot save JSON {path} {exc}")


def is_yaml(string):
    '''
    Detects if a string is a valid yaml or not
    '''
    try:
        yaml_object = yaml.safe_load(string)
    except Exception as exc:
        return False
    return True


def is_yaml_file(path):
    '''
    Detects if a file is a valid yaml file or not
    '''
    try:
        if os.path.isfile(path):
            with open(path) as f:
                if yaml.safe_load(f):
                    return True
        return False
    except Exception as exc:
        return False


def get_tap_target_names(yaml_dir):
    """Retrieves names of taps and targets inside yaml_dir.

    Args:
        yaml_dir (str): Path to the directory, which contains taps and targets files with .yml extension.

    Returns:
        (tap_yamls, target_yamls): tap_yamls is a list of names inside yaml_dir with "tap_*.yml" pattern.
                                   target_yamls is a list of names inside yaml_dir with "target_*.yml" pattern.
    """
    TAP_PREFIX = "tap_"
    TARGET_PREFIX = "target_"
    YAML_EXTENSION = ".yml"

    yamls = [f for f in os.listdir(yaml_dir) if os.path.isfile(os.path.join(yaml_dir, f)) and f.endswith(YAML_EXTENSION)]
    target_yamls = set(filter(lambda y: y.startswith(TARGET_PREFIX), yamls))
    tap_yamls = set(filter(lambda y: y.startswith(TAP_PREFIX), yamls))

    return tap_yamls, target_yamls


def load_yaml(yaml_file, vault_secret=None):
    '''
    Load a YAML file into a python dictionary.

    The YAML file can be fully encrypted by Ansible-Vault or can contain
    multiple inline Ansible-Vault encrypted values. Ansible Vault
    encryption is ideal to store passwords or encrypt the entire file
    with sensitive data if required.
    '''
    vault = VaultLib()

    if vault_secret:
        secret_file = get_file_vault_secret(filename=vault_secret, loader=DataLoader())
        secret_file.load()
        vault.secrets = [('default', secret_file)]

    data = None
    if os.path.isfile(yaml_file):
        with open(yaml_file, 'r') as stream:
            try:
                if is_encrypted_file(stream):
                    file_data = stream.read()
                    data = yaml.load(vault.decrypt(file_data, None))
                else:
                    loader = AnsibleLoader(stream, None, vault.secrets)
                    try:
                        data = loader.get_single_data()
                    except Exception as exc:
                        raise Exception(f"Error when loading YAML config at {yaml_file} {exc}")
                    finally:
                        loader.dispose()
            except yaml.YAMLError as exc:
                raise Exception(f"Error when loading YAML config at {yaml_file} {exc}")
    else:
        logger.debug(f"No file at {yaml_file}")

    return data


def vault_encrypt(plaintext, secret):
    '''
    Vault encrypt a piece of data.
    '''
    try:
        vault = VaultLib()
        secret_file = get_file_vault_secret(filename=secret, loader=DataLoader())
        secret_file.load()
        vault.secrets = [('default', secret_file)]

        return vault.encrypt(plaintext)
    except AnsibleError as e:
        logger.critical(f"Cannot encrypt string: {e}")
        sys.exit(1)


def vault_format_ciphertext_yaml(b_ciphertext, indent=None, name=None):
    '''
    Format a ciphertext to YAML compatible string
    '''
    indent = indent or 10

    block_format_var_name = ""
    if name:
        block_format_var_name = "%s: " % name

    block_format_header = "%s!vault |" % block_format_var_name
    lines = []
    vault_ciphertext = to_text(b_ciphertext)

    lines.append(block_format_header)
    for line in vault_ciphertext.splitlines():
        lines.append('%s%s' % (' ' * indent, line))

    yaml_ciphertext = '\n'.join(lines)
    return yaml_ciphertext


def load_schema(name):
    '''
    Load a json schema
    '''
    path = f"{os.path.dirname(__file__)}/schemas/{name}.json"
    schema = load_json(path)

    if not schema:
        logger.critical(f"Cannot load schema at {path}")
        sys.exit(1)

    return schema


def get_sample_file_paths():
    '''
    Get list of every available sample files (YAML, etc.) with absolute paths
    '''
    samples_dir = os.path.join(os.path.dirname(__file__), "samples")
    return search_files(samples_dir, patterns=['*.yml.sample', 'README.md'], abs_path=True)


def validate(instance, schema):
    '''
    Validate an instance under a given json schema
    '''
    try:
        # Serialise vault encrypted objects to string
        schema_safe_inst = json.loads(json.dumps(instance, cls=AnsibleJSONEncoder))
        jsonschema.validate(instance=schema_safe_inst, schema=schema)
    except Exception as exc:
        logger.critical(f"Invalid object. {exc}")
        sys.exit(1)


def delete_empty_keys(d):
    '''
    Deleting every key from a dictionary where the values are empty
    '''
    return {k: v for k, v in d.items() if v is not None}


def delete_keys_from_dict(d, keys):
    '''
    Delete specific keys from a nested dictionary
    '''
    if not isinstance(d, (dict, list)):
        return d
    if isinstance(d, list):
        return [v for v in (delete_keys_from_dict(v, keys) for v in d) if v]
    return {k: v for k, v in ((k, delete_keys_from_dict(v, keys)) for k, v in d.items()) if k not in keys}


def silentremove(path):
    '''
    Deleting file with no error message if the file not exists
    '''
    logger.debug(f'Removing file at {path}')
    try:
        os.remove(path)
    except OSError as e:

        # errno.ENOENT = no such file or directory
        if e.errno != errno.ENOENT:
            raise


def search_files(search_dir, patterns=['*'], sort=False, abs_path=False):
    '''
    Searching files in a specific directory that match a pattern
    '''
    files = []
    if os.path.isdir(search_dir):
        # Search files and sort if required
        p_files = []
        for pattern in patterns:
            p_files.extend(filter(os.path.isfile, glob.glob(os.path.join(search_dir, pattern))))
        if sort:
            p_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        # Cut the whole paths, we only need the filenames
        files = list(map(lambda x: os.path.basename(x) if not abs_path else x, p_files))

    return files


def extract_log_attributes(log_file):
    '''
    Extracting common properties from a log file name
    '''
    logger.debug(f'Extracting attributes from log file {log_file}')
    target_id = 'unknown'
    tap_id = 'unknown'
    timestamp = datetime.utcfromtimestamp(0).isoformat()
    sync_engine = 'unknown'
    status = 'unknown'

    try:
        # Extract attributes from log file name
        log_attr = re.search(r'(.*)-(.*)-(.*)\.(.*)\.log\.(.*)', log_file)
        target_id = log_attr.group(1)
        tap_id = log_attr.group(2)
        timestamp = datetime.strptime(log_attr.group(3), '%Y%m%d_%H%M%S').isoformat()
        sync_engine = log_attr.group(4)
        status = log_attr.group(5)

    # Ignore exception when attributes cannot be extracted - Defaults will be used
    except Exception:
        pass

    # Return as a dictionary
    return {
        'filename': log_file,
        'target_id': target_id,
        'tap_id': tap_id,
        'timestamp': timestamp,
        'sync_engine': sync_engine,
        'status': status
    }


def get_tap_property(tap, property_key):
    '''
    Get a tap specific property value
    '''
    tap_props_inst = tap_properties.get_tap_properties(tap)
    tap_props = tap_props_inst.get(tap.get('type'), tap_props_inst.get('DEFAULT', {}))

    return tap_props.get(property_key)


def get_tap_property_by_tap_type(tap_type, property_key):
    '''
    Get a tap specific property value by a tap type.

    Some attributes cannot derived only by tap type. These
    properties might not be returned as expected.
    '''
    tap_props_inst = tap_properties.get_tap_properties()
    tap_props = tap_props_inst.get(tap_type, tap_props_inst.get('DEFAULT', {}))

    return tap_props.get(property_key)


def get_tap_extra_config_keys(tap):
    '''
    '''
    return get_tap_property(tap, 'tap_config_extras')


def get_tap_stream_id(tap, database_name, schema_name, table_name):
    '''
    Generate tap_stream_id in the same format as a specific
    tap generating it. They are not consistent.

    Stream id is the string that tha tap's discovery mode puts
    into the properties.json file
    '''
    pattern = get_tap_property(tap, 'tap_stream_id_pattern')

    return pattern \
        .replace("{{database_name}}", f"{database_name}") \
        .replace("{{schema_name}}", f"{schema_name}") \
        .replace("{{table_name}}", f"{table_name}")


def get_tap_stream_name(tap, database_name, schema_name, table_name):
    '''
    Generate tap_stream_name in the same format as a specific
    tap generating it. They are not consistent.

    Stream name is the string that the tap puts into the output
    singer messages
    '''
    pattern = get_tap_property(tap, 'tap_stream_name_pattern')

    return pattern \
        .replace("{{database_name}}", f"{database_name}") \
        .replace("{{schema_name}}", f"{schema_name}") \
        .replace("{{table_name}}", f"{table_name}")


def get_tap_default_replication_method(tap):
    '''
    Get the default replication method for a tap
    '''
    return get_tap_property(tap, 'default_replication_method')


def get_fastsync_bin(venv_dir, tap_type, target_type):
    '''
    Get the absolute path of a fastsync executable
    '''
    source = tap_type.replace('tap-', '')
    target = target_type.replace('target-', '')
    fastsync_name = f"{source}-to-{target}"

    return os.path.join(venv_dir, "pipelinewise", "bin", fastsync_name)


def run_command(command, log_file=None, line_callback=None):
    """
    Runs a shell command with or without log file with STDOUT and STDERR
    """
    piped_command = f"/bin/bash -o pipefail -c '{command}'"
    logger.debug(f'Running command: {piped_command}')

    # Logfile is needed: Continuously polling STDOUT and STDERR and writing into a log file
    # Once the command finished STDERR redirects to STDOUT and returns _only_ STDOUT
    if log_file is not None:
        logger.info(f'Writing output into {log_file}')

        # Create log dir if not exists
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

        # Status embedded in the log file name
        log_file_running = f"{log_file}.running"
        log_file_failed = f"{log_file}.failed"
        log_file_success = f"{log_file}.success"

        # Start command
        proc = Popen(shlex.split(piped_command), stdout=PIPE, stderr=STDOUT)
        f = open(log_file_running, "w+")
        stdout = ''
        while True:
            line = proc.stdout.readline()
            if line:
                decoded_line = line.decode('utf-8')

                if line_callback is not None:
                    decoded_line = line_callback(decoded_line)

                stdout += decoded_line
                f.write(decoded_line)
                f.flush()
            if proc.poll() is not None:
                break

        f.close()
        rc = proc.poll()
        if rc != 0:
            # Add failed status to the log file name
            os.rename(log_file_running, log_file_failed)

            # Raise run command exception
            raise RunCommandException(f"Command failed. Return code: {rc}")
        else:
            # Add success status to the log file name
            os.rename(log_file_running, log_file_success)

        return [rc, stdout, None]

    # No logfile needed: STDOUT and STDERR returns in an array once the command finished
    else:
        proc = Popen(shlex.split(piped_command), stdout=PIPE, stderr=PIPE)
        x = proc.communicate()
        rc = proc.returncode
        stdout = x[0].decode('utf-8')
        stderr = x[1].decode('utf-8')

        if rc != 0:
          logger.error(stderr)

        return [rc, stdout, stderr]
