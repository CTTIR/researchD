// Execute the shipped dashboard against a small DOM fixture. Assigning HTML is
// forbidden: question/metadata values must remain text and control values.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.style={}; this.events={}; this.dataset={}; this._value=''; this._text=''; }
  set value(value) { this._value=String(value); }
  get value() { return this._value; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(''); }
  set innerText(value) { this.textContent=value; }
  get innerText() { return this.textContent; }
  set innerHTML(value) { throw new Error('Dashboard must not parse data as HTML'); }
  setAttribute(name,value) { this[name]=value; }
  appendChild(child) { child.parent=this; this.children.push(child); return child; }
  append(...children) { children.forEach(c=>this.appendChild(c)); }
  replaceChildren(...children) { this.children=[]; this._text=''; this.append(...children); }
  remove() { this.parent.children=this.parent.children.filter(c=>c!==this); }
  addEventListener(event,callback) { this.events[event]=callback; }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
  querySelectorAll(selector) {
    return this.children.flatMap(child=>[
      ...(selector.startsWith('.') && (child.className||'').split(' ').includes(selector.slice(1))?[child]:[]),
      ...child.querySelectorAll(selector)]);
  }
}
const template=fs.readFileSync(path.join(__dirname,'../../web/templates/dashboard.html'),'utf8');
const roots=new Map([...template.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element('div')]));
const document={
  createElement:tag=>new Element(tag), createTextNode:text=>{const node=new Element('#text');node.textContent=text;return node;},
  querySelector:selector=>roots.get(selector.slice(1))||null,
  querySelectorAll:selector=>[...roots.values()].flatMap(el=>el.querySelectorAll(selector))
};
const question={id:'ordinary',label:`Study's <img src=x onerror=alert(1)>`,from_days_back:7,enabled:true,
  concepts:[{name:'<b>topic</b>',terms:['</textarea><script>bad</script>'],mesh:[]}],
  sources:{},api_key_present:{},delivery:['file'],email_enabled:false,email_to:[]};
let failSettings=false, writes=0;
const fetch=async(url,options)=>{
  if(options?.method==='POST') writes++;
  if(url==='/api/questions') return {ok:true,json:async()=>[question]};
  if(url==='/api/source_health') return {ok:true,json:async()=>({sources:[{source:'<img src=x>',track:'lit',stale:false}]})};
  if(url.startsWith('/api/signal_config/')) return {ok:!failSettings,json:async()=>({sources:{},feed_categories:{},scrape_targets:[],api_key_present:{}})};
  throw new Error('Unexpected URL '+url);
};
const source=template.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/\{\{ base_url\|tojson \}\}/,'"http://localhost"')
  .replace(/\{\{ study_types\|tojson \}\}/,'["rct"]');
const context=vm.createContext({document,fetch,console,location:{origin:'http://localhost'},
  window:{scrollTo(){}},confirm:()=>false,alert:()=>{}});
vm.runInContext(source,context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await settle();
  const list=roots.get('qlist');
  assert.ok(list.textContent.includes(question.label));
  list.children[0].children[1].children[0].events.click();
  await settle();
  assert.equal(roots.get('qid').value,'ordinary');
  assert.equal(roots.get('qlabel').value,question.label);
  const concept=roots.get('concepts').children[0];
  assert.equal(concept.querySelector('.cname').value,question.concepts[0].name);
  assert.equal(concept.querySelector('.cterms').value,question.concepts[0].terms[0]);
  assert.ok(roots.get('healthbox').textContent.includes('<img src=x>'));
  assert.ok(roots.get('feedurls').textContent.includes('/feed/ordinary.xml'));
  failSettings=true;
  context.edit(question); await settle();
  await context.save();
  assert.equal(writes,0,'A failed signal-settings load must block saving default settings over existing data');
  assert.match(roots.get('msg').textContent,/reload/);
  console.log('Dashboard edit, hostile text, feed URLs, and failed-settings-save regressions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
