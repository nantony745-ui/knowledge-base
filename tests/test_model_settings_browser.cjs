const assert = require('node:assert/strict');
const {readFileSync, writeFileSync, mkdtempSync, existsSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {spawn} = require('node:child_process');
const {test} = require('node:test');

const chrome = process.env.CHROME_PATH || (process.platform === 'win32'
  ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : '/usr/bin/chromium');
const frontend = path.join(__dirname, '../frontend');
const script = text => `<script>${text.replace(/<\/script/gi, '<\\/script')}</script>`;

function preload() {
  window.requests = [];
  window.browserErrors = [];
  window.addEventListener('error', event => browserErrors.push(event.message));
  window.addEventListener('unhandledrejection', event => browserErrors.push(String(event.reason)));
  window.configs = Object.fromEntries(['chat', 'embedding', 'reranker'].map(service => [service,
    {model:service + '-default', url:'https://provider.invalid/v1', key_configured:true}]));
  window.fetch = async (url, options) => {
    requests.push({url, ...options});
    if (options.method === 'GET') return new Response(JSON.stringify(configs));
    if (window.hold) await new Promise(resolve => {window.release = resolve;});
    const body = JSON.parse(options.body);
    if (body.api_key === 'bad-key') return new Response(JSON.stringify({detail:{message:'服务返回 HTTP 401'}}), {status:503});
    if (options.method === 'POST') return new Response(JSON.stringify({connected:true,message:'连接有效'}));
    const service = url.split('/').at(-1);
    configs[service] = {model:body.model,url:body.url.replace(/\/embeddings$/, ''),key_configured:true};
    return new Response(JSON.stringify(configs[service]));
  };
}

async function verify() {
  const report = {checks:[], errors:[]};
  const check = (value, message) => {if (!value) throw new Error(message);};
  const until = async condition => {
    for (let i=0; i<100 && !condition(); i++) await new Promise(resolve => setTimeout(resolve, 20));
    check(condition(), '等待页面状态超时');
  };
  try {
    await until(() => !document.querySelector('#settings-content').hidden);
    check(document.querySelectorAll('.model-card').length === 3, '模型表单缺失');
    check(document.documentElement.scrollWidth <= window.innerWidth, '页面横向溢出');
    const forms = [...document.querySelectorAll('.model-card')];
    for (const form of forms) {
      check(form.elements.model.value === form.dataset.service + '-default', '默认模型加载错误');
      check(form.elements.api_key.type === 'password' && form.elements.api_key.value === '', '密钥回显');
      check(form.elements.api_key.placeholder.includes('已配置'), '密钥状态缺失');
    }
    report.checks.push('默认配置、三组表单、密钥隐藏、响应式布局');
    for (const form of forms) {
      const message = form.querySelector('[role=status]');
      form.elements.model.value = form.dataset.service + '-changed';
      form.elements.api_key.value = 'new-local-key';
      form.requestSubmit(form.querySelector('[data-action=test]'));
      await until(() => message.dataset.state === 'success');
      check(message.textContent === '连接有效', '检测结果缺失');
      check(form.elements.api_key.value === 'new-local-key', '检测后密钥丢失导致无法保存');
      const probe = requests.at(-1);
      check(probe.method === 'POST' && probe.url.endsWith('/test'), '检测错误地保存配置');
      check(JSON.parse(probe.body).api_key === 'new-local-key', '检测未使用输入密钥');
      form.requestSubmit(form.querySelector('[data-action=save]'));
      await until(() => message.dataset.state === 'success' && message.textContent.includes('已保存'));
      check(form.elements.api_key.value === '', '保存后未清除密钥输入');
      check(configs[form.dataset.service].model === form.dataset.service + '-changed', '保存模型错误');
    }
    report.checks.push('三类模型检测当前输入、独立保存、保存后清空密钥');
    const form = forms[0], message = form.querySelector('[role=status]');
    form.requestSubmit(form.querySelector('[data-action=save]'));
    await until(() => message.dataset.state === 'success');
    check(!Object.hasOwn(JSON.parse(requests.at(-1).body), 'api_key'), '留空密钥覆盖了原密钥');
    form.elements.api_key.value = 'bad-key';
    form.requestSubmit(form.querySelector('[data-action=test]'));
    await until(() => message.dataset.state === 'error');
    check(message.textContent.includes('401'), '连接失败未显示错误');
    check([...form.querySelectorAll('input,button')].every(control => !control.disabled), '失败后未恢复操作');
    window.hold = true;
    form.elements.api_key.value = 'new-local-key';
    form.requestSubmit(form.querySelector('[data-action=test]'));
    await until(() => typeof window.release === 'function');
    const count = requests.length;
    form.dispatchEvent(new SubmitEvent('submit', {bubbles:true,cancelable:true,submitter:form.querySelector('[data-action=test]')}));
    check(requests.length === count && form.querySelector('[data-action=save]').disabled, '重复点击未被拦截');
    window.release();
    await until(() => message.dataset.state === 'success');
    check(browserErrors.length === 0, browserErrors.join(';'));
    report.checks.push('留空保留密钥、错误提示、失败恢复、防重复请求');
    report.status = 'passed';
  } catch (error) {report.status = 'failed'; report.errors.push(error.message);}
  const result = document.createElement('pre');
  result.id = 'settings-verification';
  result.textContent = JSON.stringify(report);
  document.body.append(result);
}

function fixture() {
  const html = readFileSync(path.join(frontend, 'settings.html'), 'utf8')
    .replace(/<script[^>]+src=[^>]+><\/script>/g, '').replace(/<link[^>]+rel="stylesheet"[^>]+>/g, '');
  const styles = ['styles.css', 'toolbox.css', 'settings.css'].map(file => readFileSync(path.join(frontend, file), 'utf8')).join('\n');
  const scripts = script(`(${preload})();`) + script(readFileSync(path.join(frontend, 'settings.js'), 'utf8')) +
    script(`document.addEventListener('DOMContentLoaded', ${verify});`);
  return html.replace('</head>', `<style>${styles}</style></head>`).replace('</body>', scripts + '</body>');
}

function runChrome(url, directory, size) {
  return new Promise((resolve, reject) => {
    const child = spawn(chrome, ['--headless=new', '--no-first-run', '--disable-gpu', '--disable-background-networking',
      '--user-data-dir=' + path.join(directory, 'profile'), '--window-size=' + size, '--virtual-time-budget=12000',
      '--dump-dom', url]);
    let output = '', done = false;
    const finish = (error, value) => {
      if (done) return;
      done = true; clearTimeout(timer); child.kill(); child.stdout.destroy(); child.stderr.destroy(); child.unref();
      error ? reject(error) : resolve(value);
    };
    const timer = setTimeout(() => finish(new Error('Chrome验证超时')), 25000);
    child.on('error', error => finish(error)); child.stderr.resume();
    child.stdout.on('data', chunk => {
      output += chunk;
      const match = output.match(/<pre id="settings-verification">([^<]*)<\/pre>/);
      if (match) finish(null, JSON.parse(match[1].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')));
    });
    child.on('close', () => {if (!done) finish(new Error('Chrome未返回验证结果'));});
  });
}

for (const size of ['1360,1000', '390,844']) test(`模型设置 Chrome 浏览器回归 ${size}`, {skip:!existsSync(chrome)}, async () => {
  const directory = mkdtempSync(path.join(tmpdir(), 'kb-model-settings-'));
  const file = path.join(directory, 'settings.html');
  writeFileSync(file, fixture());
  const report = await runChrome(pathToFileURL(file).href, directory, size);
  assert.equal(report.status, 'passed', JSON.stringify(report));
  console.log(JSON.stringify({size, ...report}));
});
