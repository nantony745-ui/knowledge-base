// 消息渲染测试的 DOM 桩；真实排版、滚动与视口位置由 Chrome 回归验证。
const voidTags = new Set(['BR', 'HR', 'IMG', 'INPUT', 'META', 'LINK']);

function node(type, name, value = '') {
  const item = {nodeType: type, nodeName: name, tagName: name, nodeValue: value,
    parentNode: null, childNodes: [], attrs: new Map(), listeners: {}, dataset: {}};
  const changed = () => {
    let ancestor = item;
    while (ancestor) { ancestor.onHTML?.(ancestor.innerHTML); ancestor = ancestor.parentNode; }
  };
  Object.defineProperties(item, {
    nodeValue: {get: () => value, set: text => { value = text; changed(); }},
    children: {get: () => item.childNodes.filter(child => child.nodeType === 1)},
    firstChild: {get: () => item.childNodes[0] || null},
    nextSibling: {get: () => item.parentNode?.childNodes[item.parentNode.childNodes.indexOf(item) + 1] || null},
    attributes: {get: () => [...item.attrs].map(([name, value]) => ({name, value}))},
    innerHTML: {configurable: true, get: () => item.childNodes.map(serialize).join(''), set: html => {
      item.childNodes.forEach(child => { child.parentNode = null; });
      item.childNodes = parse(html).childNodes;
      item.childNodes.forEach(child => { child.parentNode = item; });
      changed();
    }},
    textContent: {configurable: true, get: () => type === 3 ? item.nodeValue : item.childNodes.map(child => child.textContent).join(''), set: text => {
      item.childNodes = text ? [node(3, '#text', text)] : [];
      item.childNodes.forEach(child => { child.parentNode = item; });
    }},
    open: {get: () => item.hasAttribute('open'), set: open => open ? item.setAttribute('open', '') : item.removeAttribute('open')}
  });
  item.classList = {contains: name => (item.getAttribute('class') || '').split(' ').includes(name), toggle() {}, add() {}};
  item.getAttribute = name => item.attrs.get(name) ?? null;
  item.hasAttribute = name => item.attrs.has(name);
  item.setAttribute = (name, value) => {
    item.attrs.set(name, String(value));
    if (name.startsWith('data-')) item.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
  };
  item.removeAttribute = name => { item.attrs.delete(name); };
  item.insertBefore = (child, before) => {
    child.remove();
    const index = before ? item.childNodes.indexOf(before) : item.childNodes.length;
    item.childNodes.splice(index, 0, child); child.parentNode = item; changed();
  };
  item.remove = () => {
    if (!item.parentNode) return;
    const parent = item.parentNode;
    parent.childNodes.splice(parent.childNodes.indexOf(item), 1); item.parentNode = null;
    parent.onHTML?.(parent.innerHTML);
  };
  item.append = child => item.insertBefore(child, null);
  item.replaceChildren = () => { item.childNodes = []; };
  item.addEventListener = (name, handler) => { item.listeners[name] = handler; };
  item.getBoundingClientRect = () => ({top: 0, bottom: 30});
  item.querySelectorAll = selector => selector.startsWith('.markdown-body') ? item.blocks || [] : [];
  return item;
}

function serialize(item) {
  if (item.nodeType === 3) return item.nodeValue;
  const attrs = [...item.attrs].map(([name, value]) => ` ${name}="${value}"`).join('');
  const tag = item.nodeName.toLowerCase();
  return `<${tag}${attrs}>${voidTags.has(item.nodeName) ? '' : item.innerHTML + `</${tag}>`}`;
}

function parse(html) {
  const root = node(11, '#document-fragment');
  const stack = [root];
  for (const token of String(html).match(/<[^>]*>|[^<]+/g) || []) {
    if (token.startsWith('</')) { stack.pop(); continue; }
    let child;
    if (token.startsWith('<')) {
      const tag = token.match(/^<([\w-]+)/);
      if (!tag) continue;
      child = node(1, tag[1].toUpperCase());
      for (const attr of token.slice(tag[0].length).matchAll(/([\w:-]+)(?:="([^"]*)")?/g)) child.setAttribute(attr[1], attr[2] || '');
    } else child = node(3, '#text', token);
    const parent = stack[stack.length - 1];
    parent.childNodes.push(child); child.parentNode = parent;
    if (child.nodeType === 1 && !voidTags.has(child.nodeName) && !token.endsWith('/>')) stack.push(child);
  }
  return root;
}

function findAttribute(root, name, value) {
  for (const child of root.children) {
    if (child.getAttribute(name) === value) return child;
    const match = findAttribute(child, name, value);
    if (match) return match;
  }
  return null;
}

module.exports = {node, parse, findAttribute};
