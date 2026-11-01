// Run with an isolated jsdom installation; no production dependency is added.
// node frontend/tests/citation-dom.mjs data/citation-dom-check
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import vm from 'node:vm';
import { build } from 'esbuild';
const { JSDOM } = createRequire(resolve(process.argv[2], 'package.json'))('jsdom');
const dom = new JSDOM('<body><main id="answer"></main><section id="citation-1"></section><section id="citation-2"></section></body>', {url:'http://localhost/'});
const bundle = await build({entryPoints:['frontend/src/pages/citationHtml.ts'], bundle:true, write:false, format:'cjs', platform:'browser'});
const module = {exports:{}};
vm.runInNewContext(bundle.outputFiles[0].text, {module, exports:module.exports,
  window:dom.window, document:dom.window.document, NodeFilter:dom.window.NodeFilter});
const exports = module.exports;
const response = {runtime_mode:'react_v2',answer:'First [1]. Second [2]. Again [1]. <img src="https://example.invalid" onerror="alert(1)"> [bad](javascript:alert) [link](/untrusted)',
  citation_metadata:[{number:1,title:'First source',excerpt:'Verified first',page_numbers:[3]},
    {number:2,title:'Second source',excerpt:'Verified second',page_numbers:[]}]};
const root=dom.window.document.getElementById('answer');
root.innerHTML=exports.citationHtml(response);
const links=[...root.querySelectorAll('a')];
assert.deepEqual(links.map(a=>a.getAttribute('href')),['#citation-1','#citation-2','#citation-1']);
assert.equal(links[0].title,'First source\n页码：3\nVerified first');
assert.equal(links[1].title,'Second source\nVerified second');
assert.equal(root.querySelector('img,script,[onerror]'),null);
assert.ok(!root.innerHTML.includes('javascript:'));
assert.ok(exports.citationHtml({...response,runtime_mode:'legacy',answer:'[legacy](/original)'}).includes('href="/original"'));
links[1].click(); await new Promise(r=>setTimeout(r,10));
assert.equal(dom.window.location.hash,'#citation-2');
links[0].click(); await new Promise(r=>setTimeout(r,10));
assert.equal(dom.window.location.hash,'#citation-1');
console.log('8/8 DOM assertions passed (numbering, hover text, click targets, sanitization, legacy)');
