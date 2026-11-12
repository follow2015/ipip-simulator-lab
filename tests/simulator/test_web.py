from unittest.mock import AsyncMock
import yaml
from aiohttp.test_utils import TestClient, TestServer
from idc_simulator import __version__
from idc_simulator.runtime import Runtime
from idc_simulator.tools import generate_lab


async def test_web_version_badge_is_served_not_hardcoded(tmp_path):
    runtime = Runtime(tmp_path,model_only=True)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        page = await (await client.get("/")).text()
        assert 'id="version-label"' in page
        assert "IDC Simulator / v" not in page
        script = await (await client.get("/ui/app.js")).text()
        assert "status.version" in script
        status = await (await client.get("/v1/status")).json()
        assert status["version"] == __version__
    finally:
        await client.close(); await runtime.close()


async def test_web_assets_public_api_still_authenticated(tmp_path):
    runtime = Runtime(tmp_path,model_only=True)
    client = TestClient(TestServer(runtime.app("secret")))
    await client.start_server()
    try:
        root = await client.get("/")
        assert root.status == 200 and "网络仿真管理台" in await root.text()
        assert "text/html" in root.headers["Content-Type"]
        for asset in ("app.js","styles.css"):
            assert (await client.get("/ui/"+asset)).status == 200
        assert (await client.get("/v1/status")).status == 401
        assert (await client.get("/v1/templates/gpu-idc")).status == 401
        assert (await client.get("/ui/unknown.js")).status != 200
        response = await client.get("/v1/status",headers={"Authorization":"Bearer secret"})
        assert (await response.json())["model_only"] is True
    finally:
        await client.close(); await runtime.close()


async def test_templates_import_export_and_model_only_start(tmp_path):
    runtime = Runtime(tmp_path,model_only=True)
    runtime.supervisor.start = AsyncMock()
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        template = await (await client.get("/v1/templates/gpu-idc")).json()
        assert len(template["devices"]) == 82
        local = await (await client.get("/v1/templates/local")).json()
        assert local["devices"][0]["management_ip"] == "127.0.0.1"
        response = await client.post("/v1/labs/import",json={"content":yaml.safe_dump(local)})
        assert response.status == 201
        assert (await client.post("/v1/labs/local-web/start",json={})).status == 400
        runtime.supervisor.start.assert_not_called()
        config = await (await client.get("/v1/labs/local-web/config")).json()
        assert isinstance(config["devices"],list) and isinstance(config["devices"][0]["interfaces"],list)
        assert all("oper" not in p for p in config["devices"][0]["interfaces"])
        config["name"] = "exported"
        config["devices"][0]["id"] = "other"
        config["devices"][0]["management_ip"] = "127.0.0.2"
        for h in config["hosts"]:
            h["device"] = "other"
        assert (await client.post("/v1/labs",json=config)).status == 201
        assert (await client.post("/v1/labs/import",json={"content":"bad: ["})).status == 400
        assert (await client.get("/v1/templates/missing")).status == 404
    finally:
        await client.close(); await runtime.close()


async def test_generated_topology_api_supports_scale_layouts_and_rejects_invalid_inputs(tmp_path):
    runtime = Runtime(tmp_path,model_only=True)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        for layout, count, ports, links in [('chain',12,24,11),('tree',17,24,16),('ring',8,24,8),('mixed',24,48,32)]:
            response = await client.get('/v1/templates/generated',params={'name':f'generated-{layout}','devices':count,'ports':ports,'layout':layout})
            assert response.status == 200
            topology = await response.json()
            assert len(topology['devices']) == count and len(topology['links']) == links
            assert all(device['hostname'] == device['id'] for device in topology['devices'])
            assert len({host['mac'] for host in topology['hosts']}) == len(topology['hosts'])
            host_ids = {host['id'] for host in topology['hosts']}
            assert all(flow['source'] in host_ids and flow['destination'] in host_ids for flow in topology['flows'])
            created = await client.post('/v1/labs',json=topology)
            assert created.status == 201
        assert (await client.get('/v1/templates/generated',params={'devices':241})).status == 400
        assert (await client.get('/v1/templates/generated',params={'layout':'ring','devices':2})).status == 400
        assert (await client.get('/v1/templates/generated',params={'layout':'chain','devices':3,'ports':2})).status == 400
        assert (await client.get('/v1/templates/generated',params={'local':'true','devices':2})).status == 400
        assert (await client.get('/v1/templates/generated',params={'name':'bad/name'})).status == 400
    finally:
        await client.close(); await runtime.close()


async def test_generated_topology_subnet_collision_is_avoided(tmp_path):
    runtime = Runtime(tmp_path,model_only=True)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        configs = []
        for name in ('lab-36','lab-87'):
            config = await (await client.get('/v1/templates/generated',params={'name':name,'devices':1,'ports':4})).json()
            configs.append(config)
            assert (await client.post('/v1/labs',json=config)).status == 201
        assert configs[0]['management']['subnet'] != configs[1]['management']['subnet']
        assert configs[0]['devices'][0]['management_ip'] != configs[1]['devices'][0]['management_ip']
    finally:
        await client.close(); await runtime.close()


async def test_rewire_updates_neighbors_fdb_carrier_and_rolls_back(tmp_path):
    runtime = Runtime(tmp_path,model_only=True)
    lab = generate_lab(2,5)
    runtime.controller.create(lab)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        response = await client.post("/v1/devices/sw-001/transactions",headers={"If-Match":'"1"'},json={"operations":[
            {"kind":"link","id":"link-1","fields":{"endpoints":["sw-001:p2","sw-002:p2"],"mode":"trunk","allowed_vlans":[100]}}]})
        assert response.status == 200
        a = runtime.controller.snapshot("sw-001")
        assert not a["device"]["interfaces"]["p5"]["oper"] and a["device"]["interfaces"]["p5"]["admin"]
        assert a["tables"]["lldp"][0]["ifindex"] == 2
        assert all(f["ifindex"] == 2 for f in a["tables"]["fdb"] if f["mac"] == lab["hosts"][1]["mac"])
        response = await client.post("/v1/devices/sw-001/transactions",headers={"If-Match":'"1"'},json={"operations":[{"kind":"link","id":"link-1","up":False}]})
        assert response.status == 409
        response = await client.post("/v1/devices/sw-001/transactions",headers={"If-Match":'"2"'},json={"operations":[{"kind":"link","id":"link-1","fields":{"endpoints":["sw-001:missing","sw-002:p2"]}}]})
        assert response.status == 400 and runtime.controller.snapshot("sw-001")["revision"] == 2
        assert runtime.controller.snapshot("sw-001")["device"]["interfaces"]["p2"]["oper"]
    finally:
        await client.close(); await runtime.close()


async def test_model_only_does_not_resume_protocol_workers(tmp_path):
    first = Runtime(tmp_path,model_only=True)
    first.controller.create(generate_lab(1,4))
    first.controller.lab_status("idc-scale","running")
    await first.close()
    runtime = Runtime(tmp_path,model_only=True)
    runtime.supervisor.start = AsyncMock()
    try:
        await runtime.start()
        runtime.supervisor.start.assert_not_called()
    finally:
        await runtime.close()
