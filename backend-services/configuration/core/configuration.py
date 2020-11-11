import copy
import difflib
import multiprocessing as mp
import os
import re
import shutil
import time
from io import StringIO
from ipaddress import ip_network as str2ip
from typing import Dict
from typing import List
from typing import NoReturn
from typing import Optional
from typing import Text
from typing import TextIO
from typing import Union

import redis
import requests
import ruamel.yaml
import ujson as json
from artemis_utils import ArtemisError
from artemis_utils import flatten
from artemis_utils import get_logger
from artemis_utils import ping_redis
from artemis_utils import RABBITMQ_URI
from artemis_utils import REDIS_HOST
from artemis_utils import redis_key
from artemis_utils import REDIS_PORT
from artemis_utils import translate_as_set
from artemis_utils import translate_asn_range
from artemis_utils import translate_rfc2622
from artemis_utils import update_aliased_list
from artemis_utils.rabbitmq_util import create_exchange
from artemis_utils.rabbitmq_util import create_queue
from kombu import Connection
from kombu import Consumer
from kombu import Producer
from kombu.mixins import ConsumerProducerMixin
from tornado.ioloop import IOLoop
from tornado.web import Application
from tornado.web import RequestHandler
from yaml import load as yload

# logger
log = get_logger()

# shared memory object locks
shared_memory_locks = {"data_worker": mp.Lock(), "config_data": mp.Lock()}

# global vars
MODULE_NAME = os.getenv("MODULE_NAME", "configuration")
DATABASE_HOST = os.getenv("DATABASE_HOST", "database")
PREFIXTREE_HOST = os.getenv("PREFIXTREE_HOST", "prefixtree")
RIPERISTAP_HOST = os.getenv("RIPERISTAP_HOST", "riperistap")
OTHER_SERVICES = [PREFIXTREE_HOST, DATABASE_HOST, RIPERISTAP_HOST]
REST_PORT = int(os.getenv("REST_PORT", 3000))


def parse(raw: Union[Text, TextIO, StringIO], yaml: Optional[bool] = False):
    """
    Parser for the configuration file or string.
    The format can either be a File, StringIO or String
    """
    try:
        if yaml:
            data = yload(raw)
        else:
            data = raw
        data = check(data)
        data["timestamp"] = time.time()
        # if raw is string we save it as-is else we get the value.
        if isinstance(raw, str):
            data["raw_config"] = raw
        else:
            data["raw_config"] = raw.getvalue()
        return data, True, None
    except Exception as e:
        log.exception("exception")
        return {"timestamp": time.time()}, False, str(e)


def check(data: Text) -> Dict:
    """
    Checks if all sections and fields are defined correctly
    in the parsed configuration.
    Raises custom exceptions in case a field or section
    is misdefined.
    """
    if data is None or not isinstance(data, dict):
        raise ArtemisError("invalid-data", data)

    sections = {"prefixes", "asns", "monitors", "rules", "autoignore"}
    for section in data:
        if section not in sections:
            raise ArtemisError("invalid-section", section)

    data["prefixes"] = {k: flatten(v) for k, v in data.get("prefixes", {}).items()}
    data["asns"] = {k: flatten(v) for k, v in data.get("asns", {}).items()}
    data["monitors"] = data.get("monitors", {})
    data["rules"] = data.get("rules", [])
    data["autoignore"] = data.get("autoignore", {})

    check_prefixes(data["prefixes"])
    check_monitors(data["monitors"])
    check_asns(data["asns"])
    check_rules(data["rules"])
    check_autoignore(data["autoignore"])
    return data


def check_prefixes(_prefixes):
    for prefix_group, prefixes in _prefixes.items():
        for prefix in prefixes:
            if translate_rfc2622(prefix, just_match=True):
                continue
            try:
                str2ip(prefix)
            except Exception:
                raise ArtemisError("invalid-prefix", prefix)


def check_asns(_asns):
    for name, asns in _asns.items():
        for asn in asns:
            if translate_asn_range(asn, just_match=True):
                continue
            if not isinstance(asn, int):
                raise ArtemisError("invalid-asn", asn)


def check_rules(_rules):
    rule_supported_fields = {
        "prefixes",
        "policies",
        "origin_asns",
        "neighbors",
        "prepend_seq",
        "mitigation",
        "community_annotations",
    }

    for rule in _rules:
        for field in rule:
            if field not in rule_supported_fields:
                log.warning("unsupported field found {} in {}".format(field, rule))
        rule["prefixes"] = flatten(rule["prefixes"])
        for prefix in rule["prefixes"]:
            if translate_rfc2622(prefix, just_match=True):
                continue
            try:
                str2ip(prefix)
            except Exception:
                raise ArtemisError("invalid-prefix", prefix)
        rule["origin_asns"] = flatten(rule.get("origin_asns", []))
        if rule["origin_asns"] == ["*"]:
            rule["origin_asns"] = [-1]
        if "neighbors" in rule and "prepend_seq" in rule:
            raise ArtemisError("neighbors-prepend_seq-mutually-exclusive", "")
        rule["neighbors"] = flatten(rule.get("neighbors", []))
        if rule["neighbors"] == ["*"]:
            rule["neighbors"] = [-1]
        rule["prepend_seq"] = list(map(flatten, rule.get("prepend_seq", [])))
        rule["mitigation"] = flatten(rule.get("mitigation", "manual"))
        rule["policies"] = flatten(rule.get("policies", []))
        rule["community_annotations"] = rule.get("community_annotations", [])
        if not isinstance(rule["community_annotations"], list):
            raise ArtemisError("invalid-outer-list-comm-annotations", "")
        seen_community_annotations = set()
        for annotation_entry_outer in rule["community_annotations"]:
            if not isinstance(annotation_entry_outer, dict):
                raise ArtemisError("invalid-dict-comm-annotations", "")
            for annotation in annotation_entry_outer:
                if annotation in seen_community_annotations:
                    raise ArtemisError("duplicate-community-annotation", annotation)
                seen_community_annotations.add(annotation)
                if not isinstance(annotation_entry_outer[annotation], list):
                    raise ArtemisError(
                        "invalid-inner-list-comm-annotations", annotation
                    )
                for annotation_entry_inner in annotation_entry_outer[annotation]:

                    for key in annotation_entry_inner:
                        if key not in ["in", "out"]:
                            raise ArtemisError("invalid-community-annotation-key", key)
                    in_communities = flatten(annotation_entry_inner.get("in", []))
                    for community in in_communities:
                        if not re.match(r"\d+\:\d+", community):
                            raise ArtemisError("invalid-bgp-community", community)
                    out_communities = flatten(annotation_entry_inner.get("out", []))
                    for community in out_communities:
                        if not re.match(r"\d+\:\d+", community):
                            raise ArtemisError("invalid-bgp-community", community)

        for asn in rule["origin_asns"] + rule["neighbors"]:
            if translate_asn_range(asn, just_match=True):
                continue
            if not isinstance(asn, int):
                raise ArtemisError("invalid-asn", asn)


def check_monitors(_monitors):
    supported_monitors = {
        "riperis",
        "exabgp",
        "bgpstreamhist",
        "bgpstreamlive",
        "bgpstreamkafka",
    }
    available_ris = {
        "rrc01",
        "rrc02",
        "rrc03",
        "rrc04",
        "rrc05",
        "rrc06",
        "rrc07",
        "rrc08",
        "rrc09",
        "rrc10",
        "rrc11",
        "rrc12",
        "rrc13",
        "rrc14",
        "rrc15",
        "rrc16",
        "rrc17",
        "rrc18",
        "rrc19",
        "rrc20",
        "rrc21",
        "rrc22",
        "rrc23",
        "rrc00",
    }
    available_bgpstreamlive = {"routeviews", "ris", "caida"}
    required_bgpstreamkafka = {"host", "port", "topic"}

    for key, info in _monitors.items():
        if key not in supported_monitors:
            raise ArtemisError("invalid-monitor", key)
        elif key == "riperis":
            if info == [""]:
                continue
            for unavailable in set(info).difference(available_ris):
                log.warning("unavailable monitor {}".format(unavailable))
        elif key == "bgpstreamlive":
            if not info or not set(info).issubset(available_bgpstreamlive):
                raise ArtemisError("invalid-bgpstreamlive-project", info)
        elif key == "bgpstreamkafka":
            if not set(info.keys()).issubset(required_bgpstreamkafka):
                raise ArtemisError(
                    "invalid-bgpstreamkakfa-configuration", list(info.keys())
                )
        elif key == "exabgp":
            for entry in info:
                if "ip" not in entry and "port" not in entry:
                    raise ArtemisError("invalid-exabgp-info", entry)
                # container service IPs will start as follows
                if not entry["ip"].startswith("exabgp"):
                    try:
                        str2ip(entry["ip"])
                    except Exception:
                        raise ArtemisError("invalid-exabgp-ip", entry["ip"])
                if not isinstance(entry["port"], int):
                    raise ArtemisError("invalid-exabgp-port", entry["port"])
                if "autoconf" in entry:
                    if entry["autoconf"] == "true":
                        entry["autoconf"] = True
                    elif entry["autoconf"] == "false":
                        del entry["autoconf"]
                    else:
                        raise ArtemisError(
                            "invalid-exabgp-autoconf-flag", entry["autoconf"]
                        )
                if "learn_neighbors" in entry:
                    if "autoconf" not in entry:
                        raise ArtemisError(
                            "invalid-exabgp-missing-autoconf-for-learn_neighbors",
                            entry["learn_neighbors"],
                        )
                    if entry["learn_neighbors"] == "true":
                        entry["learn_neighbors"] = True
                    elif entry["learn_neighbors"] == "false":
                        del entry["learn_neighbors"]
                    else:
                        raise ArtemisError(
                            "invalid-exabgp-learn_neighbors-flag",
                            entry["learn_neighbors"],
                        )
        elif key == "bgpstreamhist":
            if not isinstance(info, str) or not os.path.exists(info):
                raise ArtemisError("invalid-bgpstreamhist-dir", info)


def check_autoignore(_autoignore_rules):
    autoignore_supported_fields = {
        "thres_num_peers_seen",
        "thres_num_ases_infected",
        "interval",
        "prefixes",
    }

    for rule_key, rule in _autoignore_rules.items():
        for field in rule:
            if field not in autoignore_supported_fields:
                log.warning("unsupported field found {} in {}".format(field, rule))
        if "prefixes" not in rule:
            raise ArtemisError("no-prefixes-in-autoignore-rule", rule_key)
        rule["prefixes"] = flatten(rule["prefixes"])
        for prefix in rule["prefixes"]:
            if translate_rfc2622(prefix, just_match=True):
                continue
            try:
                str2ip(prefix)
            except Exception:
                raise ArtemisError("invalid-prefix", prefix)
        field = None
        try:
            for field in [
                "thres_num_peers_seen",
                "thres_num_ases_infected",
                "interval",
            ]:
                rule[field] = int(rule.get(field, 0))
        except Exception:
            raise ArtemisError("invalid-value-for-{}".format(field), rule.get(field, 0))


def translate_learn_rule_msg_to_dicts(raw):
    """
    Translates a learn rule message payload (raw)
    into ARTEMIS-compatible dictionaries
    :param raw:
        "key": <str>,
        "prefix": <str>,
        "type": <str>,
        "hijack_as": <int>,
    }
    :return: (<str>rule_prefix, <list><int>rule_asns,
    <list><dict>rules)
    """
    # initialize dictionaries and lists
    rule_prefix = {}
    rule_asns = {}
    rules = []

    try:
        # retrieve (origin, neighbor) combinations from redis
        redis_hijack_key = redis_key(raw["prefix"], raw["hijack_as"], raw["type"])
        hij_orig_neighb_set = "hij_orig_neighb_{}".format(redis_hijack_key)
        orig_to_neighb = {}
        neighb_to_origs = {}
        asns = set()
        redis_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
        ping_redis(redis_conn)
        if redis_conn.exists(hij_orig_neighb_set):
            for element in redis_conn.sscan_iter(hij_orig_neighb_set):
                (origin_str, neighbor_str) = element.decode().split("_")
                origin = None
                if origin_str != "None":
                    origin = int(origin_str)
                neighbor = None
                if neighbor_str != "None":
                    neighbor = int(neighbor_str)
                if origin is not None:
                    asns.add(origin)
                    if origin not in orig_to_neighb:
                        orig_to_neighb[origin] = set()
                    if neighbor is not None:
                        asns.add(neighbor)
                        orig_to_neighb[origin].add(neighbor)
                        if neighbor not in neighb_to_origs:
                            neighb_to_origs[neighbor] = set()
                        neighb_to_origs[neighbor].add(origin)

        # learned rule prefix
        rule_prefix = {
            raw["prefix"]: "LEARNED_H_{}_P_{}".format(
                raw["key"],
                raw["prefix"].replace("/", "_").replace(".", "_").replace(":", "_"),
            )
        }

        # learned rule asns
        rule_asns = {}
        for asn in sorted(list(asns)):
            rule_asns[asn] = "LEARNED_H_{}_AS_{}".format(raw["key"], asn)

        # learned rule(s)
        if re.match(r"^[E|S]\|0.*", raw["type"]):
            assert len(orig_to_neighb) == 1
            assert raw["hijack_as"] in orig_to_neighb
            learned_rule = {
                "prefixes": [rule_prefix[raw["prefix"]]],
                "origin_asns": [rule_asns[raw["hijack_as"]]],
                "neighbors": [
                    rule_asns[asn] for asn in sorted(orig_to_neighb[raw["hijack_as"]])
                ],
                "mitigation": "manual",
            }
            rules.append(learned_rule)
        elif re.match(r"^[E|S]\|1.*", raw["type"]):
            assert len(neighb_to_origs) == 1
            assert raw["hijack_as"] in neighb_to_origs
            learned_rule = {
                "prefixes": [rule_prefix[raw["prefix"]]],
                "origin_asns": [
                    rule_asns[asn] for asn in sorted(neighb_to_origs[raw["hijack_as"]])
                ],
                "neighbors": [rule_asns[raw["hijack_as"]]],
                "mitigation": "manual",
            }
            rules.append(learned_rule)
        elif re.match(r"^[E|S]\|-.*", raw["type"]) or re.match(r"^Q\|0.*", raw["type"]):
            for origin in sorted(orig_to_neighb):
                learned_rule = {
                    "prefixes": [rule_prefix[raw["prefix"]]],
                    "origin_asns": [rule_asns[origin]],
                    "neighbors": [
                        rule_asns[asn] for asn in sorted(orig_to_neighb[origin])
                    ],
                    "mitigation": "manual",
                }
                rules.append(learned_rule)
    except Exception:
        log.exception("{}".format(raw))
        return None, None, None

    return rule_prefix, rule_asns, rules


def translate_bgp_update_to_dicts(bgp_update, learn_neighbors=False):
    """
    Translates a BGP update message payload
    into ARTEMIS-compatible dictionaries
    :param learn_neighbors: <boolean> to determine if we should learn neighbors out of this update
    :param bgp_update: {
        "prefix": <str>,
        "key": <str>,
        "peer_asn": <int>,
        "path": (<int>)<list>
        "service": <str>,
        "type": <str>,
        "communities": [
            ...,
            {
                "asn": <int>,
                "value": <int>
            },
            ...,
        ]
        "timestamp" : <float>
    }
    :return: (<str>rule_prefix, <list><int>rule_asns,
    <list><dict>rules)
    """
    # initialize dictionaries and lists
    rule_prefix = {}
    rule_asns = {}
    rules = []

    try:
        if bgp_update["type"] == "A":

            # learned rule prefix
            rule_prefix = {
                bgp_update["prefix"]: "AUTOCONF_P_{}".format(
                    bgp_update["prefix"]
                    .replace("/", "_")
                    .replace(".", "_")
                    .replace(":", "_")
                )
            }

            # learned rule asns
            as_path = bgp_update["path"]
            origin_asn = None
            neighbor = None
            asns = set()
            if as_path:
                origin_asn = as_path[-1]
                asns.add(origin_asn)
            neighbors = set()
            if "communities" in bgp_update and learn_neighbors:
                for community in bgp_update["communities"]:
                    asn = int(community["asn"])
                    value = int(community["value"])
                    if asn == origin_asn and value != origin_asn:
                        neighbors.add(value)
            for neighbor in neighbors:
                asns.add(neighbor)

            rule_asns = {}
            for asn in sorted(list(asns)):
                rule_asns[asn] = "AUTOCONF_AS_{}".format(asn)

            # learned rule
            learned_rule = {
                "prefixes": [rule_prefix[bgp_update["prefix"]]],
                "origin_asns": [rule_asns[origin_asn]],
                "mitigation": "manual",
            }
            if neighbors:
                learned_rule["neighbors"] = []
                for neighbor in neighbors:
                    learned_rule["neighbors"].append(rule_asns[neighbor])
            rules.append(learned_rule)
        else:
            # learned rule prefix
            rule_prefix = {
                bgp_update["prefix"]: "AUTOCONF_P_{}".format(
                    bgp_update["prefix"]
                    .replace("/", "_")
                    .replace(".", "_")
                    .replace(":", "_")
                )
            }

    except Exception:
        log.exception("{}".format(bgp_update))
        return None, None, None

    return rule_prefix, rule_asns, rules


def get_existing_rules_from_new_rule(yaml_conf, rule_prefix, rule_asns, rule):
    try:
        # calculate origin asns for the new rule (int format)
        new_rule_origin_asns = set()
        for origin_asn_anchor in rule["origin_asns"]:

            # translate origin asn anchor into integer for quick retrieval
            origin_asn = None
            for asn in rule_asns:
                if rule_asns[asn] == origin_asn_anchor:
                    origin_asn = asn
                    break
            if origin_asn:
                new_rule_origin_asns.add(origin_asn)

        # calculate neighbors for the new rule (int format)
        new_rule_neighbors = set()
        if "neighbors" in rule and rule["neighbors"]:
            for neighbor_anchor in rule["neighbors"]:

                # translate neighbor anchor into integer for quick retrieval
                neighbor = None
                for asn in rule_asns:
                    if rule_asns[asn] == neighbor_anchor:
                        neighbor = asn
                        break
                if neighbor:
                    new_rule_neighbors.add(neighbor)

        # check existence of rule (by checking the affected prefixes, origin_asns, and neighbors)
        existing_rules_found = set()
        rule_extension_needed = set()
        if "rules" not in yaml_conf:
            yaml_conf["rules"] = ruamel.yaml.comments.CommentedSeq()
        for i, existing_rule in enumerate(yaml_conf["rules"]):
            existing_rule_prefixes = set()
            for existing_prefix_seq in existing_rule["prefixes"]:
                if isinstance(existing_prefix_seq, str):
                    existing_rule_prefixes.add(existing_prefix_seq)
                    continue
                for existing_prefix in existing_prefix_seq:
                    existing_rule_prefixes.add(existing_prefix)
            if set(rule_prefix.keys()) == existing_rule_prefixes:
                # same prefixes, proceed to origin asn checking

                # calculate the origin asns of the existing rule
                existing_origin_asns = set()
                if "origin_asns" in existing_rule:
                    for existing_origin_asn_seq in existing_rule["origin_asns"]:
                        if existing_origin_asn_seq:
                            if isinstance(existing_origin_asn_seq, int):
                                existing_origin_asns.add(existing_origin_asn_seq)
                                continue
                            for existing_origin_asn in existing_origin_asn_seq:
                                if existing_origin_asn != -1:
                                    existing_origin_asns.add(existing_origin_asn)
                if new_rule_origin_asns == existing_origin_asns:
                    # same prefixes, proceed to neighbor checking

                    # calculate the neighbors of the existing rule
                    existing_neighbors = set()
                    if "neighbors" in existing_rule:
                        for existing_neighbor_seq in existing_rule["neighbors"]:
                            if existing_neighbor_seq:
                                if isinstance(existing_neighbor_seq, int):
                                    existing_neighbors.add(existing_neighbor_seq)
                                    continue
                                for existing_neighbor in existing_neighbor_seq:
                                    if existing_neighbor != -1:
                                        existing_neighbors.add(existing_neighbor)
                    if new_rule_neighbors == existing_neighbors:
                        # existing rule found, do nothing
                        existing_rules_found.add(i)
                    elif not existing_neighbors:
                        existing_rules_found.add(i)
                        # rule extension needed if wildcarded neighbors
                        rule_extension_needed.add(i)
    except Exception:
        log.exception("exception")
        return set(), set()
    return existing_rules_found, rule_extension_needed


def get_created_prefix_anchors_from_new_rule(yaml_conf, rule_prefix):
    created_prefix_anchors = set()
    all_prefixes_exist = True
    try:
        for prefix in rule_prefix:
            prefix_anchor = rule_prefix[prefix]
            if "prefixes" not in yaml_conf:
                yaml_conf["prefixes"] = ruamel.yaml.comments.CommentedMap()
            if prefix_anchor not in yaml_conf["prefixes"]:
                all_prefixes_exist = False
                yaml_conf["prefixes"][
                    prefix_anchor
                ] = ruamel.yaml.comments.CommentedSeq()
                yaml_conf["prefixes"][prefix_anchor].append(prefix)
                created_prefix_anchors.add(prefix_anchor)
            yaml_conf["prefixes"][prefix_anchor].yaml_set_anchor(
                prefix_anchor, always_dump=True
            )
    except Exception:
        log.exception("exception")
        return set(), False
    return created_prefix_anchors, all_prefixes_exist


def get_created_asn_anchors_from_new_rule(yaml_conf, rule_asns):
    created_asn_anchors = set()
    all_asns_exist = True
    try:
        for asn in sorted(rule_asns):
            asn_anchor = rule_asns[asn]
            if "asns" not in yaml_conf:
                yaml_conf["asns"] = ruamel.yaml.comments.CommentedMap()
            if asn_anchor not in yaml_conf["asns"]:
                all_asns_exist = False
                yaml_conf["asns"][asn_anchor] = ruamel.yaml.comments.CommentedSeq()
                yaml_conf["asns"][asn_anchor].append(asn)
                created_asn_anchors.add(asn_anchor)
            yaml_conf["asns"][asn_anchor].yaml_set_anchor(asn_anchor, always_dump=True)
    except Exception:
        log.exception("exception")
        return set(), False
    return created_asn_anchors, all_asns_exist


def post_configuration_to_other_services(data):
    for service in OTHER_SERVICES:
        try:
            r = requests.post(
                url="http://{}:{}/config".format(service, REST_PORT),
                data=json.dumps(data),
            )
            response = r.json()
            assert response["success"]
        except Exception:
            log.exception("exception")
            log.error("could not configure service '{}'".format(service))


def write_conf_via_tmp_file(config_file, tmp_file, yaml_conf) -> NoReturn:
    with open(tmp_file, "w") as f:
        ruamel.yaml.dump(yaml_conf, f, Dumper=ruamel.yaml.RoundTripDumper)
    shutil.copymode(config_file, tmp_file)
    os.rename(tmp_file, config_file)


def write_raw_conf_via_tmp_file(config_file, tmp_file, raw) -> NoReturn:
    with open(tmp_file, "w") as f:
        f.write(raw)
    shutil.copymode(config_file, tmp_file)
    os.rename(tmp_file, config_file)


def translate_learn_rule_dicts_to_yaml_conf(
    yaml_conf, rule_prefix, rule_asns, rules, withdrawal=False
):
    """
    Translates the dicts from translate_learn_rule_msg_to_dicts
    function into yaml configuration,
    preserving the order and comments of the current file
    (edits the yaml_conf in-place)
    :param yaml_conf: <dict>
    :param rule_prefix: <str>
    :param rule_asns: <list><int>
    :param rules: <list><dict>
    :param withdrawal: <bool>
    :return: (<str>, <bool>)
    """

    if (withdrawal and not rule_prefix) or (
        not withdrawal and (not rule_prefix or not rule_asns or not rules)
    ):
        return "problem with rule installation", False
    try:
        if rule_prefix and withdrawal:
            rules_to_be_deleted = []
            for existing_rule in yaml_conf["rules"]:
                prefix_seqs_to_be_deleted = []
                for existing_prefix_seq in existing_rule["prefixes"]:
                    if isinstance(existing_prefix_seq, str):
                        for prefix in rule_prefix:
                            if existing_prefix_seq == prefix:
                                prefix_seqs_to_be_deleted.append(existing_prefix_seq)
                                break
                        continue
                    for existing_prefix in existing_prefix_seq:
                        for prefix in rule_prefix:
                            if existing_prefix == prefix:
                                prefix_seqs_to_be_deleted.append(existing_prefix_seq)
                                break
                if len(prefix_seqs_to_be_deleted) == len(existing_rule["prefixes"]):
                    # same prefixes, rule needs to be deleted
                    rules_to_be_deleted.append(existing_rule)
                elif prefix_seqs_to_be_deleted:
                    # only the rule prefix(es) need to be deleted
                    for prefix_seq in prefix_seqs_to_be_deleted:
                        existing_rule["prefixes"].remove(prefix_seq)
            for rule in rules_to_be_deleted:
                yaml_conf["rules"].remove(rule)
            for prefix_anchor in rule_prefix.values():
                if prefix_anchor in yaml_conf["prefixes"]:
                    del yaml_conf["prefixes"][prefix_anchor]
            return "ok", True

        # create prefix anchors
        created_prefix_anchors, prefixes_exist = get_created_prefix_anchors_from_new_rule(
            yaml_conf, rule_prefix
        )

        # create asn anchors
        created_asn_anchors, asns_exist = get_created_asn_anchors_from_new_rule(
            yaml_conf, rule_asns
        )

        # append rules
        for rule in rules:
            # declare new rules directly for non-existent prefixes (optimization)
            if prefixes_exist:
                (
                    existing_rules_found,
                    rule_update_needed,
                ) = get_existing_rules_from_new_rule(
                    yaml_conf, rule_prefix, rule_asns, rule
                )
            else:
                existing_rules_found = []
                rule_update_needed = False

            # if no existing rule, make a new one
            if not existing_rules_found:
                rule_map = ruamel.yaml.comments.CommentedMap()

                # append prefix
                rule_map["prefixes"] = ruamel.yaml.comments.CommentedSeq()
                for prefix in rule["prefixes"]:
                    rule_map["prefixes"].append(yaml_conf["prefixes"][prefix])

                # append origin asns
                rule_map["origin_asns"] = ruamel.yaml.comments.CommentedSeq()
                for origin_asn_anchor in rule["origin_asns"]:
                    rule_map["origin_asns"].append(yaml_conf["asns"][origin_asn_anchor])

                # append neighbors
                rule_map["neighbors"] = ruamel.yaml.comments.CommentedSeq()
                if "neighbors" in rule and rule["neighbors"]:
                    for neighbor_anchor in rule["neighbors"]:
                        rule_map["neighbors"].append(yaml_conf["asns"][neighbor_anchor])
                else:
                    del rule_map["neighbors"]

                # append mitigation action
                rule_map["mitigation"] = rule["mitigation"]

                yaml_conf["rules"].append(rule_map)
            # else delete any created anchors (not needed), as long as no rule update is needed
            elif not rule_update_needed:
                for prefix_anchor in created_prefix_anchors:
                    del yaml_conf["prefixes"][prefix_anchor]
                for asn_anchor in created_asn_anchors:
                    del yaml_conf["asns"][asn_anchor]
            # rule update needed (neighbors)
            else:
                for existing_rule_found in existing_rules_found:
                    rule_map = yaml_conf["rules"][existing_rule_found]
                    if "neighbors" in rule and rule["neighbors"]:
                        if existing_rule_found in rule_update_needed:
                            rule_map["neighbors"] = ruamel.yaml.comments.CommentedSeq()
                            for neighbor_anchor in rule["neighbors"]:
                                rule_map["neighbors"].append(
                                    yaml_conf["asns"][neighbor_anchor]
                                )

    except Exception:
        log.exception("{}-{}-{}".format(rule_prefix, rule_asns, rules))
        return (
            "problem with rule installation; exception during yaml processing",
            False,
        )
    return "ok", True


class LoadAsSetsHandler(RequestHandler):
    """
    REST request handler for loading AS sets.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def post(self):
        """
        Receives a "load-as-sets" message, translates the corresponding
        as anchors into lists, and rewrites the configuration
        :return:
        """
        ret_json = {}
        try:
            with open(self.shared_memory_manager_dict["config_file"], "r") as f:
                raw = f.read()
            yaml_conf = ruamel.yaml.load(
                raw, Loader=ruamel.yaml.RoundTripLoader, preserve_quotes=True
            )
            error = False
            done_as_set_translations = {}
            if "asns" in yaml_conf:
                for name in yaml_conf["asns"]:
                    as_members = []
                    # consult cache
                    if name in done_as_set_translations:
                        as_members = done_as_set_translations[name]
                    # else try to retrieve from API
                    elif translate_as_set(name, just_match=True):
                        ret_dict = translate_as_set(name, just_match=False)
                        if ret_dict["success"] and "as_members" in ret_dict["payload"]:
                            as_members = ret_dict["payload"]["as_members"]
                            done_as_set_translations[name] = as_members
                        else:
                            error = ret_dict["error"]
                            break
                    if as_members:
                        new_as_set_cseq = ruamel.yaml.comments.CommentedSeq()
                        for asn in as_members:
                            new_as_set_cseq.append(asn)
                        new_as_set_cseq.yaml_set_anchor(name)
                        update_aliased_list(
                            yaml_conf, yaml_conf["asns"][name], new_as_set_cseq
                        )

            if error:
                ret_json = {"success": False, "payload": {}, "error": error}
            elif done_as_set_translations:
                ret_json = {
                    "success": True,
                    "payload": {
                        "message": "All ({}) AS-SET translations done".format(
                            len(done_as_set_translations)
                        )
                    },
                    "error": False,
                }
            else:
                ret_json = {
                    "success": True,
                    "payload": {"message": "No AS-SET translations were needed"},
                    "error": False,
                }

            # as-sets were resolved, update configuration
            if (not error) and done_as_set_translations:
                write_conf_via_tmp_file(
                    self.shared_memory_manager_dict["config_file"],
                    self.shared_memory_manager_dict["tmp_config_file"],
                    yaml_conf,
                )

        except Exception:
            log.exception("exception")
            ret_json = {"success": False, "payload": {}, "error": "unknown"}
        finally:
            self.write(ret_json)


class HijackLearnRuleHandler(RequestHandler):
    """
    REST request handler for learning hijack rules.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def post(self):
        """
        Receives a "learn-rule" message, translates this
        to associated ARTEMIS-compatibe dictionaries,
        and adds the prefix, asns and rule(s) to the configuration
        :param message: {
            "key": <str>,
            "prefix": <str>,
            "type": <str>,
            "hijack_as": <int>,
            "action": <str> show|approve
        }
        :return: -
        """
        payload = json.loads(self.request.body)
        log.debug("payload: {}".format(payload))

        ok = False
        yaml_conf_str = ""
        try:
            # load initial YAML configuration from file
            with open(self.shared_memory_manager_dict["config_file"], "r") as f:
                raw = f.read()
                yaml_conf = ruamel.yaml.load(
                    raw, Loader=ruamel.yaml.RoundTripLoader, preserve_quotes=True
                )

            # translate the BGP update information into ARTEMIS conf primitives
            (rule_prefix, rule_asns, rules) = translate_learn_rule_msg_to_dicts(payload)

            # create the actual ARTEMIS configuration (use copy in case the conf creation fails)
            yaml_conf_clone = copy.deepcopy(yaml_conf)
            msg, ok = translate_learn_rule_dicts_to_yaml_conf(
                yaml_conf_clone, rule_prefix, rule_asns, rules
            )
            if ok:
                # update running configuration
                yaml_conf = copy.deepcopy(yaml_conf_clone)
                yaml_conf_str = ruamel.yaml.dump(
                    yaml_conf, Dumper=ruamel.yaml.RoundTripDumper
                )
            else:
                yaml_conf_str = msg

            if payload["action"] == "approve" and ok:
                # store the new configuration to file
                write_conf_via_tmp_file(
                    self.shared_memory_manager_dict["config_file"],
                    self.shared_memory_manager_dict["tmp_config_file"],
                    yaml_conf,
                )
        except Exception:
            log.exception("exception")
            ok = False
        finally:
            # reply back to the sender with the extra yaml configuration
            # message.
            self.write({"success": ok, "new_yaml_conf": yaml_conf_str})


def configure_configuration(msg, shared_memory_manager_dict):
    shared_memory_locks["config_data"].acquire()
    ret_json = {}
    try:
        type_ = msg["type"]
        raw_ = msg["content"]

        # if received config from Frontend with comment
        comment = None
        from_frontend = False
        if isinstance(raw_, dict) and "comment" in raw_:
            comment = raw_["comment"]
            del raw_["comment"]
            raw = raw_["config"]
            from_frontend = True
        else:
            raw = raw_

        if type_ == "yaml":
            stream = StringIO("".join(raw))
            data, _flag, _error = parse(stream, yaml=True)
        else:
            data, _flag, _error = parse(raw)

        # _flag is True or False depending if the new configuration was
        # accepted or not.
        if _flag:
            log.debug("accepted new configuration")
            # compare current with previous data excluding --obviously-- timestamps
            # change to sth better
            prev_data = copy.deepcopy(data)
            del prev_data["timestamp"]
            new_data = copy.deepcopy(shared_memory_manager_dict["config_data"])
            del new_data["timestamp"]
            prev_data_str = json.dumps(prev_data, sort_keys=True)
            new_data_str = json.dumps(new_data, sort_keys=True)
            if prev_data_str != new_data_str:
                shared_memory_manager_dict["config_data"] = data
                # the following needs to take place only if conf came from frontend
                # otherwise the file is already updated to the latest version!
                if from_frontend:
                    write_raw_conf_via_tmp_file(
                        shared_memory_manager_dict["config_file"],
                        shared_memory_manager_dict["tmp_config_file"],
                        shared_memory_manager_dict["raw_config"],
                    )
                if comment:
                    shared_memory_manager_dict["config_data"]["comment"] = comment

                # configure all other services with the new config
                post_configuration_to_other_services(
                    shared_memory_manager_dict["config_data"]
                )

                # Remove the comment to avoid marking config as different
                if "comment" in shared_memory_manager_dict["config_data"]:
                    del shared_memory_manager_dict["config_data"]["comment"]
                # after accepting/writing, format new configuration correctly
                with open(shared_memory_manager_dict["config_file"], "r") as f:
                    raw = f.read()
                yaml_conf = ruamel.yaml.load(
                    raw, Loader=ruamel.yaml.RoundTripLoader, preserve_quotes=True
                )
                write_conf_via_tmp_file(
                    shared_memory_manager_dict["config_file"],
                    shared_memory_manager_dict["tmp_config_file"],
                    yaml_conf,
                )

            # reply back to the sender with a configuration accepted
            # message.
            ret_json = {"success": True, "message": "configured"}
        else:
            log.debug("rejected new configuration")
            # replay back to the sender with a configuration rejected and
            # reason message.
            ret_json = {"success": False, "message": _error}
    except Exception:
        log.exception("exception")
        ret_json = {"success": False, "message": "unknown error"}
    finally:
        shared_memory_locks["config_data"].release()
        return ret_json


class ConfigHandler(RequestHandler):
    """
    REST request handler for configuration
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def get(self):
        """
        Simply provides the configuration (in the form of a JSON dict) to the requester
        """
        self.write(self.shared_memory_manager_dict["config_data"])

    def post(self):
        """
        Parses and checks if new configuration is correct.
        Replies back to the sender if the configuration is accepted
        or rejected and notifies all services if new
        configuration is used.
        https://github.com/FORTH-ICS-INSPIRE/artemis/blob/master/backend/configs/config.yaml
        sample request body:
        {
            "type": <yaml|json>,
            "content": <list|dict>
        }
        :return: {"success": True|False, "message": <message>}
        """
        try:
            msg = json.loads(self.request.body)
            self.write(configure_configuration(msg, self.shared_memory_manager_dict))
        except Exception:
            self.write(
                {"success": False, "message": "error during service configuration"}
            )


class HealthHandler(RequestHandler):
    """
    REST request handler for health checks.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def get(self):
        """
        Extract the status of a service via a GET request.
        :return: {"status" : <unconfigured|running|stopped>}
        """
        status = "stopped"
        shared_memory_locks["data_worker"].acquire()
        if self.shared_memory_manager_dict["data_worker_running"]:
            status = "running"
        shared_memory_locks["data_worker"].release()
        self.write({"status": status})


class ControlHandler(RequestHandler):
    """
    REST request handler for control commands.
    """

    def initialize(self, shared_memory_manager_dict):
        self.shared_memory_manager_dict = shared_memory_manager_dict

    def start_data_worker(self):
        shared_memory_locks["data_worker"].acquire()
        if self.shared_memory_manager_dict["data_worker_running"]:
            log.info("data worker already running")
            shared_memory_locks["data_worker"].release()
            return "already running"
        shared_memory_locks["data_worker"].release()
        mp.Process(target=self.run_data_worker_process).start()
        return "instructed to start"

    def run_data_worker_process(self):
        try:
            with Connection(RABBITMQ_URI) as connection:
                shared_memory_locks["data_worker"].acquire()
                data_worker = ConfigurationDataWorker(
                    connection, self.shared_memory_manager_dict
                )
                self.shared_memory_manager_dict["data_worker_running"] = True
                shared_memory_locks["data_worker"].release()
                log.info("data worker started")
                data_worker.run()
        except Exception:
            log.exception("exception")
        finally:
            shared_memory_locks["data_worker"].acquire()
            self.shared_memory_manager_dict["data_worker_running"] = False
            shared_memory_locks["data_worker"].release()
            log.info("data worker stopped")

    @staticmethod
    def stop_data_worker():
        shared_memory_locks["data_worker"].acquire()
        with Connection(RABBITMQ_URI) as connection:
            with Producer(connection) as producer:
                command_exchange = create_exchange("command", connection)
                producer.publish(
                    "",
                    exchange=command_exchange,
                    routing_key="stop",
                    serializer="ujson",
                )
        shared_memory_locks["data_worker"].release()
        message = "instructed to stop"
        return message

    def post(self):
        """
        Instruct a service to start or stop by posting a command.
        Sample request body
        {
            "command": <start|stop>
        }
        :return: {"success": True|False, "message": <message>}
        """
        try:
            msg = json.loads(self.request.body)
            command = msg["command"]
            # start/stop data_worker
            if command == "start":
                message = self.start_data_worker()
                self.write({"success": True, "message": message})
            elif command == "stop":
                message = self.stop_data_worker()
                self.write({"success": True, "message": message})
            else:
                self.write({"success": False, "message": "unknown command"})
        except Exception:
            log.exception("Exception")
            self.write({"success": False, "message": "error during control"})


class Configuration:
    """
    Configuration REST Service.
    """

    def __init__(self):
        # initialize shared memory
        shared_memory_manager = mp.Manager()
        self.shared_memory_manager_dict = shared_memory_manager.dict()
        self.shared_memory_manager_dict["data_worker_running"] = False
        self.shared_memory_manager_dict["config_file"] = "/etc/artemis/config.yaml"
        self.shared_memory_manager_dict[
            "tmp_config_file"
        ] = "/etc/artemis/config.yaml.tmp"
        self.shared_memory_manager_dict["config_data"] = {}

        log.info("service initiated")

    def make_rest_app(self):
        return Application(
            [
                (
                    "/config",
                    ConfigHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/control",
                    ControlHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/health",
                    HealthHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/loadAsSets",
                    LoadAsSetsHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
                (
                    "/hijackLearnRule",
                    HijackLearnRuleHandler,
                    dict(shared_memory_manager_dict=self.shared_memory_manager_dict),
                ),
            ]
        )

    def start_rest_app(self):
        app = self.make_rest_app()
        app.listen(REST_PORT)
        log.info("REST worker started and listening to port {}".format(REST_PORT))
        IOLoop.current().start()


class ConfigurationDataWorker(ConsumerProducerMixin):
    """
    RabbitMQ Consumer/Producer for the Configuration Service.
    """

    def __init__(
        self, connection: Connection, shared_memory_manager_dict: Dict
    ) -> NoReturn:
        self.connection = connection
        self.shared_memory_manager_dict = shared_memory_manager_dict
        self.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
        ping_redis(self.redis)

        # EXCHANGES
        self.autoconf_exchange = create_exchange("autoconf", connection, declare=True)
        self.command_exchange = create_exchange("command", connection, declare=True)

        # QUEUES
        self.autoconf_update_queue = create_queue(
            MODULE_NAME,
            exchange=self.autoconf_exchange,
            routing_key="update",
            priority=4,
            random=True,
        )
        self.stop_queue = create_queue(
            MODULE_NAME, exchange=self.command_exchange, routing_key="stop", priority=1
        )

        log.info("data worker initiated")

    def get_consumers(self, Consumer: Consumer, channel: Connection) -> List[Consumer]:
        return [
            Consumer(
                queues=[self.autoconf_update_queue],
                on_message=self.handle_autoconf_updates,
                prefetch_count=1,
                accept=["ujson"],
            ),
            Consumer(
                queues=[self.stop_queue],
                on_message=self.stop_consumer_loop,
                prefetch_count=100,
                accept=["ujson"],
            ),
        ]

    def handle_autoconf_updates(self, message):
        """
        Receives a "autoconf-update" message batch, translates the corresponding
        BGP updates into ARTEMIS configuration and rewrites the configuration
        :param message:
        :return:
        """
        if not message.acknowledged:
            message.ack()
        try:
            bgp_updates = message.payload
            if not isinstance(bgp_updates, list):
                bgp_updates = [bgp_updates]

            # load initial YAML configuration from file
            with open(self.shared_memory_manager_dict["config_file"], "r") as f:
                raw = f.read()
                yaml_conf = ruamel.yaml.load(
                    raw, Loader=ruamel.yaml.RoundTripLoader, preserve_quotes=True
                )

            # save initial file content to ensure that it has not changed while processing
            with open(self.shared_memory_manager_dict["config_file"], "r") as f:
                initial_content = f.readlines()

            # process the autoconf updates
            conf_needs_update = False
            updates_processed = True
            for bgp_update in bgp_updates:
                # if you have seen the exact same update before, do nothing
                if self.redis.get(bgp_update["key"]):
                    return
                if self.redis.exists(
                    "autoconf-update-keys-to-process"
                ) and not self.redis.sismember(
                    "autoconf-update-keys-to-process", bgp_update["key"]
                ):
                    return
                learn_neighbors = False
                if "learn_neighbors" in bgp_update and bgp_update["learn_neighbors"]:
                    learn_neighbors = True
                # translate the BGP update information into ARTEMIS conf primitives
                (rule_prefix, rule_asns, rules) = translate_bgp_update_to_dicts(
                    bgp_update, learn_neighbors=learn_neighbors
                )

                # check if withdrawal (which may mean prefix/rule removal)
                withdrawal = False
                if bgp_update["type"] == "W":
                    withdrawal = True

                # create the actual ARTEMIS configuration (use copy in case the conf creation fails)
                msg, ok = translate_learn_rule_dicts_to_yaml_conf(
                    yaml_conf, rule_prefix, rule_asns, rules, withdrawal=withdrawal
                )
                if ok:
                    # update running configuration
                    conf_needs_update = True
                else:
                    log.error("!!!PROBLEM with rule autoconf installation !!!!!")
                    log.error(msg)
                    log.error(bgp_update)
                    # remove erroneous update from circulation
                    if self.redis.exists("autoconf-update-keys-to-process"):
                        redis_pipeline = self.redis.pipeline()
                        redis_pipeline.srem(
                            "autoconf-update-keys-to-process", bgp_update["key"]
                        )
                        redis_pipeline.execute()
                    # cancel operation, write nothing (this is done for optimization, even if we miss some updates)
                    conf_needs_update = False
                    updates_processed = False
                    break

            # store the updated configuration to file
            if conf_needs_update:
                # check final file content to ensure that it has not changed while processing
                with open(self.shared_memory_manager_dict["config_file"], "r") as f:
                    final_content = f.readlines()
                changes = "".join(difflib.unified_diff(initial_content, final_content))
                if changes:
                    log.info(
                        "Configuration file changed while processing autoconf updates, "
                        "re-running autoconf to avoid overwrites"
                    )
                    self.handle_autoconf_updates(message)
                    return
                write_conf_via_tmp_file(
                    self.shared_memory_manager_dict["config_file"],
                    self.shared_memory_manager_dict["tmp_config_file"],
                    yaml_conf,
                )

            # acknowledge the processing of autoconf BGP updates using redis
            if updates_processed and self.redis.exists(
                "autoconf-update-keys-to-process"
            ):
                for bgp_update in bgp_updates:
                    redis_pipeline = self.redis.pipeline()
                    redis_pipeline.srem(
                        "autoconf-update-keys-to-process", bgp_update["key"]
                    )
                    redis_pipeline.execute()
        except Exception:
            log.exception("exception")

    def stop_consumer_loop(self, message: Dict) -> NoReturn:
        """
        Callback function that stop the current consumer loop
        """
        message.ack()
        self.should_stop = True


if __name__ == "__main__":
    # initiate configuration service with REST
    configurationService = Configuration()

    # reads and parses initial configuration file
    shared_memory_locks["config_data"].acquire()
    try:
        with open(
            configurationService.shared_memory_manager_dict["config_file"], "r"
        ) as f:
            raw = f.read()
            configurationService.shared_memory_manager_dict[
                "config_data"
            ], _flag, _error = parse(raw, yaml=True)
            # configure all other services with the current config
            post_configuration_to_other_services(
                configurationService.shared_memory_manager_dict["config_data"]
            )
    except Exception:
        log.exception("exception")
    finally:
        shared_memory_locks["config_data"].release()

    # start REST within main process
    configurationService.start_rest_app()
