"""Unit tests for app/builtin_tools.py (list_argocd_applications)."""

import json

import httpx
import pytest

from app import builtin_tools as bt


def _app(name, sync="Synced", health="Healthy", conditions=None, op=None):
    status = {"sync": {"status": sync}, "health": {"status": health}}
    if conditions:
        status["conditions"] = conditions
    if op:
        status["operationState"] = {"phase": op}
    # Heavy fields a full Application read carries; the summary must drop them.
    status["resources"] = [{"kind": "Deployment", "name": f"r{i}"} for i in range(50)]
    return {
        "metadata": {"name": name, "annotations": {"x": "y" * 500}},
        "status": status,
    }


def _paged_transport(apps, page_size, seen):
    """Fake API server honouring limit/continue, recording each request."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert (
            request.url.path
            == "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"
        )
        assert int(request.url.params["limit"]) == page_size
        start = int(request.url.params.get("continue", "0"))
        items = apps[start : start + page_size]
        meta = {}
        if start + page_size < len(apps):
            meta["continue"] = str(start + page_size)
        return httpx.Response(200, json={"items": items, "metadata": meta})

    return httpx.MockTransport(handler)


def test_lists_all_pages_and_summarizes():
    apps = [_app(f"app-{i:03d}") for i in range(181)]
    apps[57] = _app("app-057", health="Degraded")
    seen = []
    client = httpx.Client(
        base_url="https://k8s.example", transport=_paged_transport(apps, 100, seen)
    )
    rows, truncated = bt.list_applications(client, "argocd", page_size=100)
    assert len(seen) == 2  # 181 apps in 2 pages
    assert not truncated
    assert len(rows) == 181
    assert set(rows[0]) == {"name", "sync", "health"}

    result = bt.build_result(rows, "hub", "argocd", False, 0, 500, truncated)
    assert result["total"] == 181
    assert result["not_synced_or_not_healthy"] == 1
    assert result["problems"] == [
        {"name": "app-057", "sync": "Synced", "health": "Degraded"}
    ]
    assert len(result["rows"]) == 181
    assert "next_offset" not in result
    # Compact: the whole answer is far smaller than one full Application object.
    assert len(json.dumps(result)) < 15_000


def test_problem_classification_and_condition():
    rows = [
        bt._summarize(_app("ok")),
        bt._summarize(_app("oos", sync="OutOfSync")),
        bt._summarize(
            _app(
                "unk",
                sync="Unknown",
                conditions=[{"type": "ComparisonError", "message": "m" * 999}],
            )
        ),
        bt._summarize(_app("prog", health="Progressing", op="Running")),
        bt._summarize({"metadata": {"name": "nostatus"}}),
    ]
    result = bt.build_result(rows, "hub", "argocd", True, 0, 500, False)
    names = [p["name"] for p in result["problems"]]
    assert names == ["nostatus", "oos", "prog", "unk"]
    unk = next(p for p in result["problems"] if p["name"] == "unk")
    assert unk["condition"].startswith("ComparisonError: ")
    assert len(unk["condition"]) <= len("ComparisonError: ") + bt._MESSAGE_CAP
    prog = next(p for p in result["problems"] if p["name"] == "prog")
    assert prog["operation"] == "Running"
    nostatus = next(p for p in result["problems"] if p["name"] == "nostatus")
    assert (nostatus["sync"], nostatus["health"]) == ("Unknown", "Unknown")
    # only_problems pages over problems only
    assert result["rows"] == [
        "nostatus Unknown Unknown",
        "oos OutOfSync Healthy",
        "prog Synced Progressing",
        "unk Unknown Healthy",
    ]


def test_row_paging():
    rows = [
        {"name": f"a{i:02d}", "sync": "Synced", "health": "Healthy"} for i in range(25)
    ]
    first = bt.build_result(rows, "hub", "argocd", False, 0, 10, False)
    assert first["returned"] == 10 and first["next_offset"] == 10
    last = bt.build_result(rows, "hub", "argocd", False, 20, 10, False)
    assert last["returned"] == 5 and "next_offset" not in last
    # limit is clamped
    assert bt.build_result(rows, "hub", "argocd", False, 0, 0, False)["returned"] == 1


def test_max_pages_sets_truncated_warning():
    apps = [_app(f"x{i}") for i in range(30)]
    client = httpx.Client(
        base_url="https://k8s.example", transport=_paged_transport(apps, 10, [])
    )
    rows, truncated = bt.list_applications(client, "argocd", page_size=10, max_pages=2)
    assert truncated and len(rows) == 20
    assert "warning" in bt.build_result(rows, "hub", "argocd", False, 0, 500, truncated)


def _text(result):
    return result["content"][0]["text"]


def _call(**kwargs):
    # DecoratedFunctionTool keeps the plain function callable.
    return bt.list_argocd_applications(**kwargs)


def test_tool_requires_cluster(monkeypatch):
    monkeypatch.delenv("ARGOCD_CLUSTER_NAME", raising=False)
    result = _call()
    assert result["status"] == "error"
    assert "cluster_name is required" in _text(result)


def test_tool_end_to_end_with_fakes(monkeypatch):
    apps = [_app("a"), _app("b", sync="OutOfSync")]
    monkeypatch.setenv("ARGOCD_CLUSTER_NAME", "hub")
    monkeypatch.setattr(
        bt, "_cluster_connection", lambda name, region: ("https://k8s.example", "ca")
    )
    monkeypatch.setattr(bt, "_eks_token", lambda name, region: "k8s-aws-v1.tok")

    def fake_client(endpoint, token, ca_pem):
        assert token == "k8s-aws-v1.tok"
        return httpx.Client(
            base_url=endpoint, transport=_paged_transport(apps, bt._PAGE_SIZE, [])
        )

    monkeypatch.setattr(bt, "_http_client", fake_client)
    result = _call()
    assert result["cluster"] == "hub"
    assert result["total"] == 2
    assert [p["name"] for p in result["problems"]] == ["b"]


@pytest.mark.parametrize("code,needle", [(403, "access entry"), (500, "HTTP 500")])
def test_tool_http_errors_are_returned_not_raised(monkeypatch, code, needle):
    monkeypatch.setattr(
        bt, "_cluster_connection", lambda name, region: ("https://k8s.example", "ca")
    )
    monkeypatch.setattr(bt, "_eks_token", lambda name, region: "t")
    transport = httpx.MockTransport(lambda req: httpx.Response(code, json={}))
    monkeypatch.setattr(
        bt,
        "_http_client",
        lambda e, t, c: httpx.Client(base_url=e, transport=transport),
    )
    result = _call(cluster_name="hub")
    assert result["status"] == "error"
    assert needle in _text(result)


def test_tool_unexpected_error_is_returned(monkeypatch):
    def boom(name, region):
        raise RuntimeError("no creds")

    monkeypatch.setattr(bt, "_cluster_connection", boom)
    result = _call(cluster_name="hub")
    assert result == {"status": "error", "content": [{"text": "RuntimeError: no creds"}]}


def test_eks_token_format():
    import boto3

    session = boto3.session.Session(
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="secret",
        region_name="us-west-2",
    )
    token = bt._eks_token("hub", "us-west-2", session=session)
    assert token.startswith("k8s-aws-v1.")
    import base64

    pad = "=" * (-len(token[11:]) % 4)
    url = base64.urlsafe_b64decode(token[11:] + pad).decode()
    assert url.startswith(
        "https://sts.us-west-2.amazonaws.com/?Action=GetCallerIdentity"
    )
    assert "x-k8s-aws-id" in url


def test_registry_and_prompt_hint(caplog):
    assert bt.builtin_tools("") == []
    assert bt.system_prompt_hint(None) == ""
    tools = bt.builtin_tools(" argocd_applications , nope ")
    assert tools == [bt.list_argocd_applications]
    assert "unknown tool" in caplog.text
    assert "list_argocd_applications" in bt.system_prompt_hint("argocd_applications")
    assert bt.list_argocd_applications.tool_name == "list_argocd_applications"


@pytest.mark.parametrize(
    "namespace",
    [
        "../../../../api/v1/namespaces/kube-system/pods?x=",
        "argocd/../kube-system",
        "ArgoCD",
        "",
        "a" * 64,
        "-argocd",
    ],
)
def test_invalid_namespace_is_rejected_before_any_call(monkeypatch, namespace):
    def no_call(*a, **k):
        raise AssertionError("must not reach the cluster")

    monkeypatch.setattr(bt, "_cluster_connection", no_call)
    result = _call(cluster_name="hub", namespace=namespace)
    assert result["status"] == "error"
    assert "DNS-1123" in _text(result)


def test_valid_namespace_passes():
    assert bt._NAMESPACE_RE.match("argocd")
    assert bt._NAMESPACE_RE.match("argo-cd-1")


def test_problems_are_capped_with_exact_counts():
    n = bt._MAX_PROBLEMS * 4
    rows = [bt._summarize(_app(f"app{i:04d}", sync="Unknown", health="Unknown")) for i in range(n)]
    result = bt.build_result(rows, "hub", "argocd", False, 0, 500, False)
    assert result["not_synced_or_not_healthy"] == n
    assert result["total"] == n
    assert len(result["problems"]) == bt._MAX_PROBLEMS
    assert result["problems_truncated"] is True
    # The names of every problem stay reachable via only_problems paging.
    paged = bt.build_result(rows, "hub", "argocd", True, 0, 2000, False)
    assert paged["returned"] == n
    assert len(json.dumps(result)) < 20_000


def test_problems_not_truncated_when_few():
    rows = [bt._summarize(_app("a")), bt._summarize(_app("b", health="Degraded"))]
    result = bt.build_result(rows, "hub", "argocd", False, 0, 500, False)
    assert result["problems_truncated"] is False
    assert [p["name"] for p in result["problems"]] == ["b"]


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"ARGOCD_CLUSTER_REGION": "eu-west-1", "AWS_REGION": "us-east-1"}, "eu-west-1"),
        ({"AWS_REGION": "us-east-1"}, "us-east-1"),
        ({"AWS_DEFAULT_REGION": "ap-southeast-2"}, "ap-southeast-2"),
        ({}, "us-west-2"),
    ],
)
def test_region_resolution(monkeypatch, env, expected):
    for k in ("ARGOCD_CLUSTER_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert bt._region() == expected


def test_error_result_is_a_strands_error_tool_result():
    # strands passes a dict with status+content through as the ToolResult as-is,
    # so the trace shows status=error instead of a success wrapping {"error": ...}.
    event = bt.list_argocd_applications._wrap_tool_result("t1", bt._error("boom"))
    assert event.tool_result == {
        "status": "error",
        "content": [{"text": "boom"}],
        "toolUseId": "t1",
    }
