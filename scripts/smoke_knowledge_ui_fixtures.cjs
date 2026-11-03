// Browser UI smoke check. Uses an existing Playwright installation; no model calls.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 require('fs').mkdirSync('/tmp/kb-ui-evidence',{recursive:true});
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const json=(route,body)=>route.fulfill({json:body});
 await page.route('**/api/auth/session',r=>json(r,{authenticated:true,username:'UI 验证',is_admin:false,permissions:['knowledge.read']}));
 await page.route('**/api/knowledge-bases',r=>json(r,[{knowledge_base_id:'fixture',name:'界面验收资料',runtime_id:'@kb:fixture',can_write_documents:false}]));
 let fail=false;
 await page.route('**/api/v2/agent/stream',async r=>{
   if(fail)return r.fulfill({status:503,json:{detail:'UI test unavailable'}});
   await r.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'result',data:{status:'completed',response:{answer:'部署需要先确认环境，再完成配置并检查运行状态。[1]',agent_name:'知识助手',tool_calls:2,runtime_mode:'react_v2',citation_validation:'passed',evidences:[{source_id:'fixture',filename:'部署操作说明.txt',location:'部署流程',retrieval_type:'lexical',content:'部署前确认环境，配置完成后检查运行状态。'}],citation_metadata:[{number:1,source_id:'fixture',title:'部署操作说明.txt',excerpt:'部署前确认环境',page_numbers:[]}],trace:[{tool:'v2.search_knowledge',status:'completed'}]}}})+'\n\n'});
 });
 await page.goto('http://127.0.0.1:8765/#/qa');
 await page.getByLabel('知识库问题').fill('部署流程是什么？');
 await page.getByRole('button',{name:'开始检索'}).click();
 await page.locator('.answer-body').waitFor();
 await page.getByText('部署操作说明.txt',{exact:true}).waitFor();
 await page.mouse.move(0,0);
 await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
 await page.screenshot({path:'/tmp/kb-ui-evidence/result-fixture.png',fullPage:true,animations:'disabled'});
 await page.getByRole('tab',{name:'执行轨迹'}).click();
 await page.getByRole('tabpanel').getByText('v2.search_knowledge',{exact:false}).waitFor();
 await page.locator('.answer-body a').click();
 await page.getByRole('tab',{name:'引用来源（1）'}).getAttribute('aria-selected').then(v=>{if(v!=='true')throw Error('citation tab not selected')});
 fail=true;
 await page.getByRole('button',{name:'开始检索'}).click();
 await page.getByRole('alert').waitFor();
 if(await page.getByLabel('知识库问题').inputValue()!=='部署流程是什么？')throw Error('question lost');
 await page.mouse.move(0,0);
 await page.evaluate(() => Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))));
 await page.screenshot({path:'/tmp/kb-ui-evidence/error-fixture.png',fullPage:true,animations:'disabled'});
 console.log('UI fixtures only: streamed result, source tab, trace tab, citation activation, failure/input retention passed');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
