// Browser UI smoke check. Uses an existing Playwright installation; no model calls.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs');
(async()=>{
const browser=await chromium.launch({headless:true,channel:'chrome'});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
await page.goto('http://127.0.0.1:8765');
await page.locator('input#username').fill(process.env.KB_TEST_USERNAME || 'experience');
await page.locator('input#password').fill(process.env.KB_TEST_PASSWORD);
await page.getByRole('button',{name:'登录工作台 →'}).click();
await page.getByRole('link',{name:'问答检索',exact:true}).click();
await page.getByRole('heading',{name:'先选择一个知识库'}).waitFor();
fs.mkdirSync('/tmp/kb-ui-evidence',{recursive:true});
await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
await page.screenshot({path:'/tmp/kb-ui-evidence/qa-desktop.png',fullPage:true,animations:'disabled'});
await page.getByRole('link',{name:'知识库',exact:true}).click();
await page.getByRole('button',{name:'新建知识库'}).click();
await page.getByText('Dify 知识库 ID（Dataset ID）',{exact:true}).waitFor();
if(await page.locator('input#embedding_model').count())throw Error('ordinary user sees embedding field');
if(await page.locator('input#tenant_key').count())throw Error('ordinary user sees tenant');
await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
await page.screenshot({path:'/tmp/kb-ui-evidence/create-desktop.png',fullPage:true,animations:'disabled'});
await page.getByRole('button',{name:'取消',exact:true}).click();
await page.getByRole('link',{name:'问答检索',exact:true}).click();
console.log('real login, QA empty state, ordinary-user fields: passed');
await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
