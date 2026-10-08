// Small DOM and formatting helpers shared by the renderer scripts.

const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = {
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v4l3 2',
  check: 'm5 12.5 4.5 4.5L19 7.5',
  checks: 'm2.5 12.5 4.5 4.5 9.5-9.5M11 16l1 1 9.5-9.5',
  relay: 'M4 12h11m0 0-4-4m4 4-4 4M19 5v14',
  alert: 'M12 8v5m0 3.5v.5M10.3 3.9 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5',
  download: 'M12 4v11m0 0-4-4m4 4 4-4M5 19h14',
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z',
  open: 'M14 4h6v6m0-6-9 9M18 14v5H5V6h5',
  close: 'M6 6l12 12M18 6 6 18',
  retry: 'M4 12a8 8 0 0 1 14-5.3L20 9m0-5v5h-5M20 12a8 8 0 0 1-14 5.3L4 15m0 5v-5h5',
};

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.title) node.title = options.title;
  if (options.type) node.type = options.type;
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) node.setAttribute(name, value);
  }
  if (options.dataset) Object.assign(node.dataset, options.dataset);
  if (options.onClick) node.addEventListener('click', options.onClick);
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function icon(name, className = 'icon') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', className);
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICONS[name] || ICONS.file);
  svg.append(path);
  return svg;
}

function iconButton(name, label, onClick, className = 'mini-button') {
  return el('button', { className, type: 'button', title: label, attrs: { 'aria-label': label }, onClick }, [icon(name)]);
}

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  return parts.slice(0, 2).map((part) => [...part][0]).join('').toUpperCase() || '?';
}

function avatarTone(nodeId) {
  let hash = 0;
  for (const character of String(nodeId || '')) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return `tone-${hash % 8}`;
}

function setAvatar(element, peer) {
  element.textContent = initials(displayName(peer));
  element.className = element.className.replace(/\btone-\d\b/g, '').trim();
  if (peer && peer.node_id) element.classList.add(avatarTone(peer.node_id));
}

function displayName(peer) {
  if (!peer) return 'Unknown peer';
  return (peer.nickname || peer.name || 'anonymous').trim();
}

function formatTime(seconds) {
  return new Date(Number(seconds) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function formatDay(seconds) {
  const date = new Date(Number(seconds) * 1000);
  const today = startOfDay(new Date());
  const day = startOfDay(date);
  if (day === today) return 'Today';
  if (day === today - 86_400_000) return 'Yesterday';
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString([], {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: sameYear ? undefined : 'numeric',
  });
}

function formatListTime(seconds) {
  if (!seconds) return '';
  const date = new Date(Number(seconds) * 1000);
  const today = startOfDay(new Date());
  const day = startOfDay(date);
  if (day === today) return formatTime(seconds);
  if (today - day < 7 * 86_400_000) return date.toLocaleDateString([], { weekday: 'short' });
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function formatRelative(seconds) {
  if (!seconds) return 'never';
  const elapsed = Math.max(0, Date.now() / 1000 - Number(seconds));
  if (elapsed < 60) return 'just now';
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)} min ago`;
  if (elapsed < 86_400) return `${Math.floor(elapsed / 3600)} h ago`;
  return formatListTime(seconds);
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB'];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size < 10 ? size.toFixed(1) : Math.round(size)} ${units[unit]}`;
}

function fileExtension(name) {
  const match = /\.([a-z0-9]{1,5})$/i.exec(String(name || ''));
  return match ? match[1].toUpperCase() : 'FILE';
}

const PREVIEW_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp']);

function isPreviewableImage(transfer) {
  return Boolean(transfer) && PREVIEW_TYPES.has(String(transfer.mime).toLowerCase());
}

function mediaUrl(filePath) {
  return `cnp2p-media://file/${encodeURIComponent(filePath)}`;
}

function newMessageId() {
  return crypto.randomUUID().replace(/-/g, '');
}

const LINK_PATTERN = /\bhttps?:\/\/[^\s<>"']+[^\s<>"'.,;:!?)\]]/gi;

// Render text with clickable web links without ever interpreting it as HTML.
function linkify(text) {
  const fragment = document.createDocumentFragment();
  let last = 0;
  for (const match of String(text).matchAll(LINK_PATTERN)) {
    if (match.index > last) fragment.append(text.slice(last, match.index));
    const url = match[0];
    fragment.append(
      el('a', {
        text: url,
        attrs: { href: '#', title: url },
        onClick: (event) => {
          event.preventDefault();
          window.cnp2p.openLink(url).catch((error) => showToast(error.message, 'error'));
        },
      }),
    );
    last = match.index + url.length;
  }
  if (last < text.length) fragment.append(text.slice(last));
  return fragment;
}

function showToast(text, type = 'info') {
  const region = document.querySelector('#toast-region');
  const toast = el('div', { className: `toast ${type}`, text });
  region.append(toast);
  while (region.children.length > 4) region.firstElementChild.remove();
  setTimeout(() => toast.remove(), type === 'error' ? 6_000 : 4_000);
}

async function copyText(text, label) {
  if (!text) return;
  await navigator.clipboard.writeText(text);
  showToast(`${label} copied`);
}
