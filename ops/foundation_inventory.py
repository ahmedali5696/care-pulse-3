"""Reviewed full-lane report inventory and private report validation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree

# Preserve the complete 0ae625c inventory and add cad3d91 Wave 3 coverage.
REPORT_COUNTS = {
    "ordinary-db": {
        "f01": 68,
        "d08": 322,
        "w3_receipt_boundary": 4,
        "w3_checkin": 8,
        "w3_checkin_races": 8,
        "w3_queue_scope": 3,
        "w3_intake": 8,
        "w3_intake_races": 15,
        "w3_admission_controls": 2,
        "w3_admission_confirmations": 8,
        "w3_admission": 19,
        "w3_admission_races": 20,
        "w3_queue_policy": 11,
        "w3_placement_races": 4,
    },
    "ordinary-stack": {
        "d06": 19,
        "b06": 61,
        "b07": 71,
        "b08": 34,
        "b09": 77,
        "b10": 143,
        "d11": 4,
        "b05-backend": 33,
        "topology-seed": 3,
        "topology-verify": 3,
        "secrets": 38,
        "health-1": 3,
        **{f"health-{i}": 1 for i in range(2, 9)},
        "auth-mail": 19,
        "sessions": 43,
        "b05-invited": 2,
        "w3_bff": 4,
        "web-ingress": 2,
        "sender": 4,
        "recovery": 27,
        "owner-invite": 1,
        "web-adapter": 6,
        "sender-mail-down": 1,
    },
    "historical": {"historical": 163},
    "compat": {
        "compat-us1": 25,
        "compat-us1-source-web-runtime": 1,
        "compat-us2": 8,
        "compat-us3": 10,
        "compat-us1-web": 9,
        "compat-us1-source-web": 2,
    },
}
TAP_REPORTS = {"web-ingress", "web-adapter", "compat-us1-web", "compat-us1-source-web"}

# JUnit case identity hashes from independent collection of the reviewed
# selectors, with the exact locked pytest 9.1.1.
EXPECTED_IDENTITY_DIGESTS = {
    "f01": "7e70af83ee86de29e997a8a1049de46482b2315984c9d78c9dc6b0f687e3ad08",
    "d08": "a7b9477b4f6bb81507290169ec7e2618063dff8d3f4d80999f4443151c022ac6",
    "w3_receipt_boundary": "0171f5704c5e9a20f845a87cb45e1ad8eaf9bf31bf359ac59e8443081cb31e55",
    "w3_checkin": "10b214226e9521ae532073188db385528ce75c4ce38b6e4387fe7265a03012c4",
    "w3_checkin_races": "9465e51c4d2cdb8828fe0be872242422f3197ecd7215881da20d78e1490d0402",
    "w3_queue_scope": "03e53d783fa21d47c7e155665d2c8d3664153485b327ed08ad585ba0f1e9cae2",
    "w3_intake": "91201fbccf23abcc9989e0a72bdb973b3be69c5d6122b6fba4e4ef2ca8b9794f",
    "w3_intake_races": "9d703696fa9cfba8c819012adaa707beb320dc5d83f4f9f40ac48374c31f23d4",
    "w3_admission_controls": "abe4eec7001932965ff19997cfdbe5a961cee8d7e38433d3d101146ee4c49a16",
    "w3_admission_confirmations": "8e553e52d606c59a21a08a1c38967ce736496d440cc80cf975cafa6ff3220285",
    "w3_admission": "17eab73e26716f671fdc13597ddf0eb92427307f1f7fb24e9e8254bf4acb9604",
    "w3_admission_races": "09ef4bfb2e5ab3dfced0431da6ca23a3f6ee85808a20dd895ee746d107af8c73",
    "w3_queue_policy": "fcbc287905c6452d4274d070005936d4c0aa25ef710159e98651ea38d6b89cc8",
    "w3_placement_races": "71f9757994c5f8808a2eac03fe2da6379db27056a6fb37c23caea82e03bc00c7",
    "w3_bff": "0ea0322495a3166b71ef64b2cc230f1122029b5e6e202a6d031050aebc10ff99",
    "d06": "67c96d46ec3790d9b4ac545cc5255e9507d0606997e9e4205e501453a493d305",
    "b06": "da894800383bf61c614d56f9bc094e0addb63a607d16856085790732646fe02f",
    "b07": "601e8005ea67693babd6c717ee794fa7ca1d2be08ec7ccac58c2e1355127388c",
    "b08": "66d4d3872b465c860267c4d1589374bec7c9c9d4139405317e621dc4953177b9",
    "b09": "d57fe50b72ad8a821bd27ac041b19873cf70efffea75929440727e3371add2a1",
    "b10": "c6e9770a8b028d00ad0472d651502c5794db422b850350a35ce194488b1ba72a",
    "d11": "c2fb29496fd2619b250027e3d7d613867f5f3c471fc40b50e06df2f59e4c0f0b",
    "b05-backend": "e42cbbcd3c03698a15822827513251226077a2c1b94257da419bf4b334158a9c",
    "secrets": "26feb87c541c876adb77f12daaee58997e18f7ae263c7cebb341fc2a28ea8898",
    "auth-mail": "3dd14d0ca3055417a7a775e17a767f7531bda104929fc9f0d2adc2997206ce65",
    "sessions": "00ce626435aafa685f0bc1124cfcb04b3bdeba46b841dbba77ab0f6f92c44242",
    "b05-invited": "77dd6494a40affbe1071cf9f4b5e17f8ef2b5892f3da5ca93b6b9f6bdf5778de",
    "sender": "d7f8954837c55cd8b7b52a3baf27cf812f47a9e5c5c56fa50f74451ed17f51cb",
    "recovery": "240e51eb5caad7e5489c0aa97e8444f95d62d250e87f45e21587f1a3fe1498a8",
    "sender-mail-down": "e7e8709cf9fd1fb8383cac1be504ef3da9425a966a27c76f1098802eeb7b9211",
    "topology-seed": "b0763c01f485d644af976d1e22e4d687fc1c5bbb62b110305a6d761369c896fd",
    "topology-verify": "b0763c01f485d644af976d1e22e4d687fc1c5bbb62b110305a6d761369c896fd",
    "health-1": "78b7e67dfe6b91b4ea94f1dba5ee97fb2b4a8e871647794eb50d88e71b4a9321",
    **{
        f"health-{i}": "3b10006353cd901890a95acd125214cca76ad6e000489bf551686815e739b3ac"
        for i in range(2, 9)
    },
    "compat-us1": "b857e4343f002dc54a02c3e91bd0b6bba95704baf5e9c4c13f6db7dce727f81e",
    "compat-us1-source-web-runtime": "448c3768a045129047d55ee22f5d143b2d265c028d9117e47f54273051d56c63",
    "compat-us2": "5e707339d7f0327d871b123bc6f37afd8831451353c91fb001cc438c25a947de",
    "compat-us3": "45d8d3d8eb26b926d41ce51262bd602c4d9bf6efa2b57bad64fc74012163c128",
    "historical": "234544850cb48a49c92748bc42127aecda79825e30f7558745c7d137a8d0333e",
    "web-ingress": "1f786157c5856c0d95d1205d2e9424af5baa5a7110719fa94a1f7a9015bae772",
    "web-adapter": "076cd699ba7d1382def035d4f76f3e3cfdbe3021eb1ef0a618fa75e29524dee4",
    "compat-us1-web": "56bf3abe5e030b43996161d25ef372f6982b13175e2e3371ea7d028238124864",
    "compat-us1-source-web": "7f965a08f728299d64ec8ddea08163fba944b38f8745c22125e53e29461380a7",
    "owner-invite": "5dd15213f56ee574c6b17b372cbd34565e12cbb39278fbfcdb4b7b1ee1e573af",
}


def _digest(document: object) -> str:
    canonical = json.dumps(
        document, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid or incomplete mandatory report coverage")


def _junit_identities(path: Path, count: int) -> list[tuple[str, str]]:
    root = ElementTree.parse(path).getroot()
    _require(root.tag in {"testsuite", "testsuites"})
    suites = [root] if root.tag == "testsuite" else list(root)
    _require(bool(suites) and all(suite.tag == "testsuite" for suite in suites))
    identities = []
    for suite in suites:
        cases = suite.findall("testcase")
        _require(int(suite.get("tests", "-1")) == len(cases))
        _require(
            all(suite.get(field) == "0" for field in ("errors", "failures", "skipped"))
        )
        for case in cases:
            _require(
                not any(
                    case.find(tag) is not None
                    for tag in ("error", "failure", "skipped")
                )
            )
            classname, name = case.get("classname"), case.get("name")
            _require(bool(classname) and bool(name))
            identities.append((classname, name))
    _require(len(identities) == count and len(set(identities)) == count)
    if root.tag == "testsuites":
        _require(
            all(
                root.get(field, expected) == expected
                for field, expected in (
                    ("tests", str(count)),
                    ("errors", "0"),
                    ("failures", "0"),
                    ("skipped", "0"),
                )
            )
        )
    return identities


def _tap_identities(path: Path, count: int) -> list[str]:
    report = path.read_text(encoding="utf-8")
    summaries = re.findall(
        r"(?m)^# (tests|pass|fail|cancelled|skipped|todo) ([0-9]+)$", report
    )
    _require(
        len(summaries) == 6
        and dict(summaries)
        == {
            "tests": str(count),
            "pass": str(count),
            "fail": "0",
            "cancelled": "0",
            "skipped": "0",
            "todo": "0",
        }
    )
    cases = re.findall(r"(?m)^[ ]*ok [0-9]+ - (.+)$", report)
    _require(len(cases) == count and len(set(cases)) == count)
    _require(not re.search(r"(?im)^[ ]*(?:not ok\b|ok\b.*# (?:skip|todo)\b)", report))
    return cases


def _report_digest(path: Path, count: int) -> str:
    _require(
        path.is_file()
        and not path.is_symlink()
        and 0 < path.stat().st_size <= 32 * 1024 * 1024
    )
    try:
        identities = (
            _tap_identities(path, count)
            if path.suffix == ".tap"
            else _junit_identities(path, count)
        )
    except (ElementTree.ParseError, UnicodeError, ValueError) as error:
        raise ValueError("invalid mandatory report") from error
    return _digest(sorted(identities))


def expected_evidence(lane: str) -> dict:
    counts = REPORT_COUNTS[lane]
    return {
        "manifest_sha256": _digest(
            {
                "counts": REPORT_COUNTS,
                "identity_digests": EXPECTED_IDENTITY_DIGESTS,
                "tap_reports": sorted(TAP_REPORTS),
            }
        ),
        "report_counts": counts,
        "total": sum(counts.values()),
    }


def collect(lane: str, folder: Path) -> dict:
    """Validate the independently specified inventory before reports are destroyed."""
    _require(lane in REPORT_COUNTS)
    _require(
        folder.is_dir()
        and not any(path.is_symlink() for path in (folder, *folder.parents))
    )
    expected = expected_evidence(lane)
    filenames = {
        f"{name}.{'tap' if name in TAP_REPORTS else 'xml'}": name
        for name in expected["report_counts"]
    }
    _require({path.name for path in folder.iterdir()} == set(filenames))
    evidence = {
        **expected,
        "report_identity_sha256": {
            name: _report_digest(folder / filename, expected["report_counts"][name])
            for filename, name in filenames.items()
        },
    }
    validate_evidence(lane, evidence)
    return evidence


def validate_evidence(lane: str, evidence: object) -> None:
    """Validate the sanitized evidence again at both seal and aggregate boundaries."""
    expected = expected_evidence(lane)
    _require(
        type(evidence) is dict
        and set(evidence) == set(expected) | {"report_identity_sha256"}
    )
    _require(
        type(evidence["manifest_sha256"]) is str
        and evidence["manifest_sha256"] == expected["manifest_sha256"]
    )
    _require(type(evidence["total"]) is int and evidence["total"] == expected["total"])
    counts = evidence["report_counts"]
    _require(type(counts) is dict and counts == expected["report_counts"])
    _require(all(type(count) is int for count in counts.values()))
    digests = evidence["report_identity_sha256"]
    _require(type(digests) is dict and set(digests) == set(counts))
    _require(
        all(
            type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in digests.values()
        )
    )
    _require(all(digests[name] == EXPECTED_IDENTITY_DIGESTS[name] for name in digests))
