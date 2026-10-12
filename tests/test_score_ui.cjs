const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
class Node{
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.classList={toggle(){}};this.value='';}
  setAttribute(k,v){this[k]=v;}
  addEventListener(){}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  set textContent(value){this.text=String(value);this.children=[];}
  get textContent(){return this.text??this.children.map(c=>c.textContent??String(c)).join('');}
}
const context=vm.createContext({console,performance:{now:()=>0},setInterval(){},
  document:{getElementById(){return new Node('div');},addEventListener(){},
    createElement:t=>new Node(t),createTextNode:t=>({textContent:t})}});
const src=fs.readFileSync('source/static/app.js','utf8').split("for(let btn of document.querySelectorAll('.nav'))")[0];
vm.runInContext(src,context);
context.fixture={symbol:'WUSDT',price:100,change_pct:0,quote_volume:10000,coverage:'hazır',
  unified:{direction:null,status:'BEKLE',score:45,
    components:{ai:18.3,indicators:17,higher_timeframe:15,patterns:0,tested_setup:0,risk_penalty:5},
    reason:'Emir defterinde 100 USDT sanal işlem için yeterli alış/satış tutarı yok',
    execution_quality:{min_top_notional_usdt:51,reference_departure_atr:.82,reference_departure_limit_atr:.75,
      assumptions:{hypothetical_notional_usdt:100},checks:[{id:'liquidity',passed:false},{id:'departure',passed:false}]}}};
const row=vm.runInContext('rowCells(fixture,true)',context);
assert.equal(row.children.length,11);
assert.match(row.children[6].textContent,/AI 18,3/);
assert.match(row.children[6].textContent,/Risk kesintisi −5/);
assert.doesNotMatch(row.children[6].textContent,/100 USDT|ATR/);
assert.match(row.children[7].textContent,/51.*100 USDT/);
assert.match(row.children[7].textContent,/0,82.*0,75 ATR/);
assert.equal(row.children[7].className,'small decision-reason');
const css=fs.readFileSync('source/static/style.css','utf8');
assert.match(css,/\.decision-reason\{white-space:pre-line/);
assert.match(css,/\.score-explanation\{white-space:normal/);
console.log('Score explanation / decision reason regression checks passed');
