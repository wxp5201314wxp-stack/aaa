const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const context={state:{products:[]},filter:'',search:'',categoryFilter:'',yuan:n=>'¥'+(n/100).toFixed(2)};
vm.createContext(context);
for(const name of ['activeProducts','filteredProducts','groupMovements','inventorySummary','outboundFinancials']){
  const start=source.indexOf('function '+name+'('),next=source.indexOf('\nfunction ',start+1);
  vm.runInContext(source.slice(start,next<0?undefined:next),context);
}
const lines=[{id:1,order_id:1,kind:'out',cost_amount:6000,freight:500,amount:10000,price_pending:0},{id:2,order_id:1,kind:'out',cost_amount:2000,freight:0,amount:3000,price_pending:0}];
let group=context.groupMovements(lines)[0];
let html=context.outboundFinancials(group);
assert(html.includes('商品成本 ¥80.00'));
assert(html.includes('含运费总成本 ¥85.00'));
assert(html.includes('销售金额 ¥130.00'));
assert(html.includes('利润 ¥45.00'));
lines[1].price_pending=1;
html=context.outboundFinancials(context.groupMovements(lines)[0]);
assert(html.includes('利润：待补价'));
assert(!html.includes('利润 ¥'));
lines[1].price_pending=0;lines[0].amount=0;lines[1].amount=0;
assert(context.outboundFinancials(context.groupMovements(lines)[0]).includes('利润 ¥-85.00'));
context.state.products=[{name:'茶',sku:'A',category:'饮料',stock:2,cost:99999,inventory_value:3000,archived:0},{name:'饼',sku:'B',category:'食品',stock:3,cost:99999,inventory_value:2000,archived:0}];
context.categoryFilter='饮料';
html=context.inventorySummary();
assert(html.includes('库存金额合计 <strong>¥50.00'));
assert(html.includes('当前筛选金额 <strong>¥30.00'));
console.log('整单成本、运费、利润、待补价、零售价和类别金额汇总校验通过');
