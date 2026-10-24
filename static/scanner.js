/* html5-qrcode 2.3.8 is bundled locally: no CDN or image upload required. */
let barcodeReader=null, scanBusy=false, scanClosing=false, scanAccepted=false, scanCallback=null;
const scannerDialog=document.querySelector('#scanner-dialog');
const scanStatus=document.querySelector('#scan-status');
const scanCamera=document.querySelector('#scan-camera');
const scanFile=document.querySelector('#scan-file');
function openBarcodeScanner(callback){
  scanCallback=callback;scanAccepted=false;scanClosing=false;
  scanFile.value='';scanCamera.disabled=false;scanFile.disabled=false;
  scanStatus.textContent=window.isSecureContext?'点击开启摄像头，或选择条码照片。':'当前使用 HTTP，无法实时调用摄像头。请使用「拍照 / 选择图片」。';
  scanCamera.disabled=!window.isSecureContext||!navigator.mediaDevices?.getUserMedia;
  scannerDialog.showModal();
}
function getBarcodeReader(){
  if(!barcodeReader){
    if(typeof Html5Qrcode==='undefined')throw Error('扫码组件加载失败，请刷新页面重试');
    barcodeReader=new Html5Qrcode('barcode-reader',{formatsToSupport:[Html5QrcodeSupportedFormats.EAN_13,Html5QrcodeSupportedFormats.EAN_8,Html5QrcodeSupportedFormats.UPC_A,Html5QrcodeSupportedFormats.UPC_E,Html5QrcodeSupportedFormats.CODE_128,Html5QrcodeSupportedFormats.CODE_39,Html5QrcodeSupportedFormats.ITF],verbose:false});
  }
  return barcodeReader;
}
async function stopBarcodeCamera(){if(barcodeReader?.isScanning)await barcodeReader.stop()}
async function closeBarcodeScanner(){
  if(scanBusy||scanClosing)return;
  scanClosing=true;
  try{await stopBarcodeCamera();if(barcodeReader)barcodeReader.clear();barcodeReader=null;scannerDialog.close()}
  catch(e){scanStatus.textContent='摄像头关闭失败，请重试关闭或刷新页面。'}
  finally{scanClosing=false}
}
async function acceptBarcode(raw){
  if(scanAccepted||scanBusy||scanClosing)return;
  const code=String(raw).trim();
  if(!code||code.length>200){scanStatus.textContent='条码内容无效，请重试';return}
  scanAccepted=true;
  await closeBarcodeScanner();
  if(!scannerDialog.open)scanCallback(code);
  else scanAccepted=false;
}
scanCamera.onclick=async()=>{
  scanBusy=true;scanCamera.disabled=true;scanFile.disabled=true;
  scanStatus.textContent='正在请求摄像头权限，请允许访问…';
  try{
    await getBarcodeReader().start({facingMode:'environment'},{fps:10},text=>{acceptBarcode(text)},()=>{});
    scanStatus.textContent='请将完整条码放入画面，保持清晰、避免反光。';
  }catch(e){scanStatus.textContent='无法启动摄像头，请检查浏览器权限，或改用拍照 / 选择图片。';scanCamera.disabled=false}
  finally{scanBusy=false;scanFile.disabled=false}
};
scanFile.onchange=async()=>{
  const file=scanFile.files[0];if(!file||scanBusy)return;
  scanBusy=true;scanCamera.disabled=true;scanFile.disabled=true;
  scanStatus.textContent='正在识别照片…';
  let result;
  try{await stopBarcodeCamera();result=await getBarcodeReader().scanFile(file,false)}
  catch(e){scanStatus.textContent='未识别到条码。请裁剪到完整条码区域，保持横向清晰、避免反光后重试。'}
  finally{scanBusy=false;scanCamera.disabled=!window.isSecureContext||!navigator.mediaDevices?.getUserMedia;scanFile.disabled=false;scanFile.value=''}
  if(result)await acceptBarcode(result);
};
document.querySelector('#scan-close').onclick=closeBarcodeScanner;
scannerDialog.addEventListener('cancel',e=>{e.preventDefault();closeBarcodeScanner()});
document.querySelector('#scan-search').onclick=()=>openBarcodeScanner(code=>{
  go('products');search=code;document.querySelector('#search').value=code;updateResults();
  toast(state.products.some(p=>p.sku===code)?'已找到商品':'未找到该编码，可在新增商品中扫码建档');
});
window.addEventListener('pagehide',()=>{if(barcodeReader?.isScanning)stopBarcodeCamera().catch(()=>{})});
