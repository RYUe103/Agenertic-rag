// Read-only desktop checks. Supply an authorized session cookie file; never commit it.
const fs = require('fs');
const assert = require('assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  if (!process.env.UI_SESSION_COOKIES) throw Error('UI_SESSION_COOKIES is required');
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  try {
    const context = await browser.newContext({viewport:{width:1440,height:900}});
    await context.addCookies(JSON.parse(fs.readFileSync(process.env.UI_SESSION_COOKIES,'utf8')));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = process.env.UI_BASE_URL || 'http://127.0.0.1:8765';
    const routes = ['', 'documents', 'upload', 'tasks', 'knowledge', 'qa', 'tools', 'integrations', 'agent-runs', 'sandbox', 'agents', 'models', 'capabilities', 'audit', 'monitoring', 'access', 'organizations'];
    for (const route of routes) {
      await page.goto(`${base}/#/${route}`);
      await page.locator('.page-heading h2').waitFor();
      await page.waitForLoadState('networkidle');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `overflow: ${route}`);
      assert.ok(await page.locator('.page-heading h2').evaluate(el => parseFloat(getComputedStyle(el).fontSize)) >= 24, `heading: ${route}`);
    }
    await page.goto(`${base}/#/knowledge`);
    await page.waitForLoadState('networkidle');
    await page.getByRole('button',{name:'新建知识库'}).waitFor();
    await page.getByLabel('搜索知识库',{exact:true}).fill('__ui_no_match__');
    await page.getByText('没有匹配的知识库，请调整搜索条件。').waitFor();
    await page.getByLabel('搜索知识库',{exact:true}).fill('');
    await page.getByRole('button',{name:'新建知识库'}).click();
    await page.getByText('Dify 知识库 ID（Dataset ID）',{exact:true}).waitFor();
    assert.equal(await page.locator('input#knowledge_base_id').count(),0);
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await page.goto(`${base}/#/access`);
    await page.getByRole('tab',{name:'角色与权限',exact:true}).click();
    await page.getByRole('button',{name:'保存角色',exact:true}).waitFor();
    await page.getByRole('tab',{name:'LDAP 认证',exact:true}).click();
    await page.getByRole('button',{name:'保存 LDAP 配置',exact:true}).waitFor();
    await page.goto(`${base}/#/organizations`);
    await page.locator('summary').filter({hasText:'绑定成员'}).click();
    await page.getByRole('button',{name:'绑定',exact:true}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('17 desktop routes, headings, overflow, KB search/create fields, access tabs, organization binding form: passed. No writes performed.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exit(1);});
