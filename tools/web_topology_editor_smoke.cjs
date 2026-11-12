const {chromium} = require(process.env.IDC_WEB_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const base = process.env.IDC_WEB_URL || 'http://127.0.0.1:8765';

(async () => {
  const browser = await chromium.launch({headless:true,...(process.env.IDC_WEB_CHROMIUM ? {executablePath:process.env.IDC_WEB_CHROMIUM} : {})});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.stack || error.message));
  const lab = `visual-${Date.now().toString(36)}`;
  try {
    await page.goto(base);
    await page.locator('#create-button').click();
    await page.locator('#template-select').selectOption('generated');
    await page.locator('#new-lab-name').fill(lab);
    await page.locator('[name=devices]').fill('240');
    await page.locator('[name=ports]').fill('512');
    assert.equal(await page.locator('#generator-scale-warning').isVisible(),true);
    await page.locator('#generator-scale-confirm').check();
    await page.locator('#create-dialog .close-dialog').first().click();
    await page.locator('#create-button').click();
    assert.equal(await page.locator('#generator-scale-warning').isVisible(),true);
    assert.equal(await page.locator('#generator-scale-confirm').isChecked(),false);
    await page.locator('#create-form button[type=submit]').click();
    assert.match(await page.locator('#create-error').textContent(),/请先确认/);
    await page.locator('[name=devices]').fill('4');
    await page.locator('[name=ports]').fill('8');
    await page.locator('[name=layout]').selectOption('chain');
    await page.locator('#create-form button[type=submit]').click();
    await page.waitForFunction(() => document.querySelector('#stat-devices').textContent === '4');
    assert.equal((await page.locator('#stat-links').textContent()).trim(),'3');

    await page.locator('[data-view=config]').click();
    await page.locator('#replace-button').click();
    await page.locator('#visual-editor-toggle').click();
    await page.waitForFunction(() => document.querySelectorAll('#topology-editor-svg .editor-node').length === 4);
    const originalJson = await page.locator('#replace-content').inputValue();
    await page.locator('#replace-content').fill('{broken json');
    assert.equal(await page.locator('#topology-editor-svg .editor-node').count(),0);
    assert.match(await page.locator('#editor-error').textContent(),/拓扑 JSON 无效/);
    assert.equal(await page.locator('#editor-add-link').isDisabled(),true);
    await page.locator('#replace-content').fill(originalJson);
    assert.equal(await page.locator('#topology-editor-svg .editor-node').count(),4);
    const firstNode = page.locator('#topology-editor-svg .editor-node').first();
    const box = await firstNode.boundingBox();
    await page.mouse.move(box.x+30,box.y+20);
    await page.mouse.down();
    await page.mouse.move(box.x+85,box.y+55,{steps:4});
    await page.mouse.up();
    assert.equal(await page.locator('#topology-editor-svg .editor-node').count(),4);
    await page.locator('#editor-reset-layout').click();
    const originalTransform = await firstNode.getAttribute('transform');
    const resetBox = await firstNode.boundingBox();
    await page.mouse.move(resetBox.x+30,resetBox.y+20);
    await page.mouse.down();
    await page.mouse.move(10000,10000,{steps:4});
    await page.mouse.up();
    assert.equal(await firstNode.evaluate(node=>{
      const box=node.getBBox(), svg=node.ownerSVGElement, transform=node.transform.baseVal.consolidate().matrix;
      return transform.e+box.width<=svg.viewBox.baseVal.width && transform.f+box.height<=svg.viewBox.baseVal.height;
    }),true);
    await page.locator('#editor-reset-layout').click();
    assert.equal(await firstNode.getAttribute('transform'),originalTransform);

    await page.locator('#editor-a-device').selectOption({index:0});
    await page.locator('#editor-a-port').selectOption('p2');
    await page.locator('#editor-b-device').selectOption({index:1});
    await page.locator('#editor-b-port').selectOption('p2');
    await page.locator('#editor-blocked').check();
    await page.locator('#editor-add-link').click();
    assert.equal(await page.locator('#topology-editor-svg .editor-edge').count(),4);
    const geometry = await page.locator('#topology-editor-svg [data-editor-link]').evaluateAll(groups => groups.map(g => {
      const hit=g.querySelector('.editor-edge-hit'), label=g.querySelector('text');
      return {id:g.dataset.editorLink,y:Number(hit.getAttribute('y1')),labelY:Number(label.getAttribute('y'))};
    }));
    const l1=geometry.find(g=>g.id==='link-1'), l4=geometry.find(g=>g.id==='link-4');
    assert.ok(l1 && l4, `parallel edges missing: ${JSON.stringify(geometry)}`);
    assert.ok(Math.abs(l1.y-l4.y)>=12, `parallel lines overlap: ${JSON.stringify(geometry)}`);
    assert.ok(Math.abs(l1.labelY-l4.labelY)>=12, `parallel labels overlap: ${JSON.stringify(geometry)}`);
    const label1=await page.locator('#topology-editor-svg [data-editor-link="link-1"] text').boundingBox();
    const label4=await page.locator('#topology-editor-svg [data-editor-link="link-4"] text').boundingBox();
    assert.ok(label1.y+label1.height<=label4.y || label4.y+label4.height<=label1.y,`parallel label boxes overlap: ${JSON.stringify({label1,label4,geometry})}`);
    await page.locator('#topology-editor-svg [data-editor-link="link-4"] text').click();
    assert.equal(await page.locator('#topology-editor-svg [data-editor-link="link-4"].selected').count(),1);
    const linePoint = await page.locator('#topology-editor-svg [data-editor-link="link-1"] .editor-edge-visible').evaluate(line => {
      const svg=line.ownerSVGElement, p=svg.createSVGPoint();
      p.x=(Number(line.getAttribute('x1'))+Number(line.getAttribute('x2')))/2;
      p.y=(Number(line.getAttribute('y1'))+Number(line.getAttribute('y2')))/2;
      const screen=p.matrixTransform(line.getScreenCTM());
      return {x:screen.x,y:screen.y};
    });
    await page.mouse.click(linePoint.x,linePoint.y);
    assert.equal(await page.locator('#topology-editor-svg [data-editor-link="link-1"].selected').count(),1);
    await page.locator('#editor-reset-layout').click();
    assert.equal(await page.locator('#topology-editor-svg .editor-edge.selected').count(),0);
    assert.equal(await page.locator('#editor-remove-link').isDisabled(),true);
    await page.locator('#replace-form button[type=submit]').click();
    await page.waitForFunction(() => !document.querySelector('#replace-apply').disabled);
    await page.locator('#replace-apply').click();
    await page.waitForFunction(() => document.querySelector('#stat-links').textContent.trim() === '4');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({lab,generator:true,drag:true,add_link:true,parallel_link:true,preview_replace:true,browser_errors:errors},null,2));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
