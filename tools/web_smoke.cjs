const {chromium} = require(process.env.IDC_WEB_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = process.env.IDC_WEB_URL || 'http://127.0.0.1:8765';
const output = process.env.IDC_WEB_REPORT_DIR || 'docs';

(async () => {
  const browser = await chromium.launch({headless:true,...(process.env.IDC_WEB_CHROMIUM ? {executablePath:process.env.IDC_WEB_CHROMIUM} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1050}});
  const errors = [], checks = [];
  page.on('pageerror', e => errors.push(e.stack || e.message));
  const check = (name) => checks.push(name);
  const nav = async (view) => page.locator(`.nav-item[data-view="${view}"]`).click();
  const selectDevicePort = async (deviceId, portId) => {
    await nav('devices');
    await page.locator('#device-search').fill(deviceId);
    await page.locator(`#device-rows [data-device="${deviceId}"]`).click();
    await page.locator('#port-select').selectOption(portId);
  };
  const selectSwitch = async () => {
    await selectDevicePort('infer-sw-1','p1');
  };
  try {
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#stat-devices').textContent === '82');
    assert.equal(await page.locator('#stat-links').textContent(),'280');
    assert.equal(await page.locator('#start-button').isDisabled(),true);
    assert.equal(await page.locator('svg .graph-node').count(),82);
    check('82 devices / 280 links and model-only controls');

    await page.locator('#fabric-filter').selectOption('inference');
    assert.equal(await page.locator('svg .graph-node').count(),68);
    await page.locator('#fabric-filter').selectOption('all');
    await page.locator('svg [data-device="infer-sw-1"]').click();
    await page.locator('#port-select').selectOption('p1');
    check('Fabric filtering and SVG device selection');

    await page.locator('#port-form input[name="admin"]').uncheck();
    const downResponse = page.waitForResponse(r => r.url().endsWith('/transactions') && r.request().method() === 'POST');
    await page.locator('#port-form button[type="submit"]').click();
    assert.equal((await downResponse).status(),200);
    await page.waitForFunction(() => document.querySelector('#stat-health-detail').textContent.includes('1 DOWN'));
    check('Port shutdown writes a real revisioned transaction');

    await nav('diagnostics');
    await page.locator('#ping-form input[name="source"]').fill('gpu-001');
    await page.locator('#ping-form input[name="destination"]').fill('10.10.0.74');
    await page.locator('#ping-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#ping-result').textContent.includes('丢包 100%'));
    check('Simulated ping reflects port-down state');

    await page.locator('#port-form input[name="admin"]').check();
    await page.locator('#port-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#stat-health-detail').textContent.includes('0 DOWN'));
    await page.locator('#ping-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#ping-result').textContent.includes('双向可达'));
    check('Port recovery restores round-trip ping');

    await page.locator('#locate-form input[name="address"]').fill('10.10.0.74');
    await page.locator('#locate-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#locate-result').textContent.includes('infer-sw-4 · p16'));
    check('IP lookup identifies the server-facing switch port');
    await page.locator('#locate-kind').selectOption('port');
    await page.locator('#locate-device').selectOption('infer-sw-4');
    await page.locator('#locate-form input[name="port"]').fill('p16');
    await page.locator('#locate-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#locate-result').textContent.includes('10.10.0.74'));
    check('Port reverse lookup displays its learned IP');

    await nav('events');
    assert.match(await page.locator('#event-rows').textContent(),/linkDown/);
    assert.match(await page.locator('#event-rows').textContent(),/linkUp/);
    check('Link alerts appear in the event table');

    await nav('links');
    await page.locator('#link-search').fill('infer-isl-1');
    await page.locator('#link-rows tr').click();
    await page.locator('#link-form input[name="up"]').uncheck();
    const linkResponse = page.waitForResponse(r => r.url().endsWith('/transactions') && r.request().method() === 'POST');
    await page.locator('#link-form button[type="submit"]').click();
    assert.equal((await linkResponse).status(),200);
    await page.waitForFunction(() => document.querySelector('#link-rows').textContent.includes('DOWN'));
    await page.locator('#link-form input[name="up"]').check();
    await page.locator('#link-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#link-rows').textContent.includes('UP'));
    check('LAG link down/up through the link editor');

    await selectSwitch();
    await page.locator('#port-form input[name="description"]').fill('draft preserved');
    const snapshot = await (await page.request.get(base+'/v1/devices/infer-sw-1/state')).json();
    await page.request.post(base+'/v1/devices/infer-sw-1/transactions',{headers:{'If-Match':`"${snapshot.revision}"`},data:{operations:[{kind:'interface',port:'p1',fields:{description:'external change'}}]}});
    const conflictResponse = page.waitForResponse(r => r.url().endsWith('/transactions') && r.request().method() === 'POST');
    await page.locator('#port-form button[type="submit"]').click();
    assert.equal((await conflictResponse).status(),409);
    assert.equal(await page.locator('#port-form input[name="description"]').inputValue(),'draft preserved');
    await page.locator('[data-action="reload-inspector"]').click();
    await page.waitForFunction(() => document.querySelector('#port-form input[name="description"]').value === 'external change');
    check('Revision conflicts preserve draft and require explicit refresh');

    await page.locator('#inspector [data-tab="network"]').click();
    await page.locator('#vlan-form input[name="vlan"]').fill('999');
    await page.locator('#vlan-form input[name="name"]').fill('browser-check');
    await page.locator('#vlan-form button').first().click();
    await page.waitForFunction(() => document.querySelector('#inspector-tab').textContent.includes('999 · browser-check'));
    check('VLAN form creates a VLAN in running state');

    await page.locator('#create-button').click();
    await page.locator('#template-select').selectOption('local');
    await page.locator('#create-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#lab-select').value === 'local-web');
    assert.equal(await page.locator('#stat-devices').textContent(),'1');
    await page.locator('#lab-select').selectOption('idc-64gpu');
    await page.waitForFunction(() => document.querySelector('#stat-devices').textContent === '82');
    check('Template creation and switching between labs');

    await page.locator('#lab-select').selectOption('local-web');
    await page.waitForFunction(() => document.querySelector('#stat-devices').textContent === '1');
    await nav('config');
    await page.locator('#replace-button').click();
    await page.waitForSelector('#replace-dialog[open]');
    const replacement = JSON.parse(await page.locator('#replace-content').inputValue());
    replacement.devices[0].hostname = 'BROWSER-REPLACED';
    await page.locator('#replace-content').fill(JSON.stringify(replacement));
    assert.equal(await page.locator('#replace-apply').isDisabled(),true);
    await page.locator('#replace-form button[type="submit"]').click();
    await page.waitForFunction(() => document.querySelector('#replace-preview').textContent.includes('修改设备：'));
    await page.locator('#replace-content').fill(JSON.stringify({...replacement, contact:'draft'}));
    assert.equal(await page.locator('#replace-apply').isDisabled(),true);
    await page.locator('#replace-content').fill(JSON.stringify(replacement));
    await page.locator('#replace-form button[type="submit"]').click();
    await page.waitForFunction(() => !document.querySelector('#replace-apply').disabled);
    const replaceResponse = page.waitForResponse(r => r.url().endsWith('/local-web/config') && r.request().method() === 'PUT');
    await page.locator('#replace-apply').click();
    assert.equal((await replaceResponse).status(),200);
    await page.waitForFunction(() => !document.querySelector('#replace-dialog').open);
    const replaced = await (await page.request.get(base+'/v1/labs/local-web/config')).json();
    assert.equal(replaced.devices[0].hostname,'BROWSER-REPLACED');
    check('Full configuration preview, invalidation and stopped-lab replacement');
    await page.waitForFunction(() => !document.querySelector('#delete-button').disabled && !document.querySelector('#replace-form button[type="submit"]').disabled);
    await page.locator('#delete-button').click();
    await page.locator('#delete-name').fill('incorrect');
    await page.locator('#delete-form button[type="submit"]').click();
    assert.match(await page.locator('#delete-error').textContent(),/不匹配/);
    await page.locator('#delete-name').fill('local-web');
    const deleteResponse = page.waitForResponse(r => r.url().endsWith('/v1/labs/local-web') && r.request().method() === 'DELETE');
    await page.locator('#delete-form button[type="submit"]').click();
    assert.equal((await deleteResponse).status(),200);
    await page.waitForFunction(() => document.querySelector('#lab-select').value === 'idc-64gpu');
    check('Deletion requires matching lab name and removes the lab');

    await nav('config');
    const downloaded = page.waitForEvent('download');
    await page.locator('#export-button').click();
    const download = await downloaded;
    const exported = JSON.parse(fs.readFileSync(await download.path(),'utf8'));
    assert.equal(exported.devices.length,82);
    assert.equal(exported.schema_version,1);
    check('Export downloads an importable topology JSON');

    const labId = await page.locator('#lab-select').inputValue();
    const topology = await (await page.request.get(`${base}/v1/labs/${encodeURIComponent(labId)}/topology`)).json();
    const candidates = topology.devices.filter(d => Object.values(d.interfaces).some(p => !p.members));
    const targetDevice = candidates.find(d => /switch/i.test(d.role || '')) || candidates[0];
    assert.ok(targetDevice, 'lab exposes a device with a physical interface');
    const targetPort = Object.values(targetDevice.interfaces).find(p => !p.members);
    const deviceId = targetDevice.id, portId = targetPort.id;
    const payload = '<img src=x onerror="window.idcXss=1">';
    const latest = await (await page.request.get(`${base}/v1/devices/${deviceId}/state`)).json();
    assert.equal((await page.request.post(`${base}/v1/devices/${deviceId}/transactions`,{headers:{'If-Match':`"${latest.revision}"`},data:{operations:[{kind:'interface',port:portId,fields:{description:payload}}]}})).status(),200);
    await page.locator('#refresh-button').click();
    await selectDevicePort(deviceId, portId);
    await page.waitForFunction(() => document.querySelector('#port-form input[name="description"]').value.includes('<img'));
    assert.equal(await page.locator('#inspector img').count(),0);
    assert.equal(await page.evaluate(() => window.idcXss),undefined);
    const xssState = await (await page.request.get(`${base}/v1/devices/${deviceId}/state`)).json();
    await page.request.post(`${base}/v1/devices/${deviceId}/transactions`,{headers:{'If-Match':`"${xssState.revision}"`},data:{operations:[{kind:'interface',port:portId,fields:{description:''}}]}});
    check('Device-provided HTML is rendered as text');

    await page.locator('#refresh-button').click();
    await nav('overview');
    await page.locator('svg [data-device="infer-sw-1"]').click();
    await page.locator('#port-select').selectOption('p1');
    fs.mkdirSync(output,{recursive:true});
    await page.locator('#fabric-filter').selectOption('inference');
    await page.evaluate(() => document.querySelector('#toast').hidden = true);
    await page.screenshot({path:`${output}/web-overview.png`,fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:`${output}/web-mobile.png`,fullPage:true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    check('Desktop and mobile layouts without horizontal overflow');
    assert.deepEqual(errors,[]);
    check('No browser JavaScript errors');
    fs.writeFileSync(`${output}/web-browser-report.json`,JSON.stringify({url:base,passed:checks.length,checks,browser_errors:errors},null,2));
    console.log(JSON.stringify({passed:checks.length,browser_errors:errors,output},null,2));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
