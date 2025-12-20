from __future__ import annotations

from pathlib import Path

from lanser.sarif import diagnostics_to_sarif


def test_diagnostics_to_sarif_converts_range(tmp_path: Path) -> None:
    bundle = {
        "kind": "diagnostics",
        "environment": {
            "workspace": str(tmp_path),
            "workspaceSnapshotId": "sha256:example",
            "positionEncoding": "utf-16",
            "languageServer": {"serverInfo": {"name": "pyright", "version": "1.1.407"}},
        },
        "result": {
            "diagnostics": [
                {
                    "uri": "file:///workspace/pkg/mod.py",
                    "range": {
                        "start": {"line": 0, "character": 3},
                        "end": {"line": 0, "character": 9},
                    },
                    "message": "Example diagnostic",
                    "severity": "error",
                    "code": "test-code",
                    "source": "pyright",
                    "tags": [1, 2],
                    "relatedInformation": [
                        {
                            "uri": "file:///workspace/pkg/mod.py",
                            "range": {
                                "start": {"line": 1, "character": 0},
                                "end": {"line": 1, "character": 4},
                            },
                            "message": "Related location",
                        }
                    ],
                }
            ]
        },
    }

    log = diagnostics_to_sarif(bundle)
    payload = log.model_dump(by_alias=True)

    assert payload["version"] == "2.1.0"
    assert payload["$schema"].endswith("sarif-2.1.0.json")

    run = payload["runs"][0]
    assert run["columnKind"] == "utf16CodeUnits"
    assert run["properties"]["workspace"] == str(tmp_path)

    result = run["results"][0]
    assert result["level"] == "error"
    assert result["ruleId"] == "test-code"
    location = result["locations"][0]
    region = location["physicalLocation"]["region"]
    assert region["startLine"] == 1
    assert region["startColumn"] == 4
    assert region["endColumn"] == 10
    related = result["relatedLocations"][0]
    related_region = related["physicalLocation"]["region"]
    assert related_region["startLine"] == 2
    assert related_region["endColumn"] == 5
