// Общие помощники интерфейса. Всё, что пришло от пользователя или сервера,
// вставляем через textContent — это исключает XSS даже без дополнительного экранирования.

export const $ = (selector, root = document) => root.querySelector(selector);

/** h('a', { href, class: 'x', onclick }, 'текст', h('b', {}, '…')) */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** Заменить содержимое узла. В отличие от replaceChildren, не превращает null в строку «null». */
export function fill(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = h('textarea', { class: 'visually-hidden', 'aria-hidden': 'true' });
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  }
}

let toastRegion = null;
export function toast(message, kind = 'info') {
  toastRegion ??= document.body.appendChild(h('div', { class: 'toast-region', role: 'status', 'aria-live': 'polite' }));
  const node = h('div', { class: `toast${kind === 'error' ? ' toast--error' : ''}` }, message);
  toastRegion.append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 6000 : 3200);
}

export function copyButton(text, label = 'Скопировать', done = 'Скопировано') {
  return h(
    'button',
    {
      type: 'button',
      class: 'btn btn--quiet btn--small',
      onclick: async () => toast((await copyText(text)) ? done : 'Не удалось скопировать — выделите текст вручную'),
    },
    label,
  );
}

export function setBusy(button, busy, busyText) {
  if (busy) {
    button.dataset.label ??= button.textContent;
    button.setAttribute('aria-busy', 'true');
    button.disabled = true;
    fill(button, h('span', { class: 'spinner', 'aria-hidden': 'true' }), busyText ?? button.dataset.label);
  } else {
    button.removeAttribute('aria-busy');
    button.disabled = false;
    if (button.dataset.label) button.textContent = button.dataset.label;
  }
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
