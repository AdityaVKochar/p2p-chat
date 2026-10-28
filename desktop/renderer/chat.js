// Conversation view: message list, attachments, and the image lightbox.

const GROUP_WINDOW_SECONDS = 5 * 60;
const ACTIVE_TRANSFER = new Set(['waiting', 'sending', 'receiving']);

function renderConversationHeader() {
  const peer = state.peers.get(state.selectedId);
  if (!peer) return;
  setAvatar(elements.selectedAvatar, peer);
  elements.selectedName.textContent = displayName(peer);
  elements.selectedVerified.classList.toggle('hidden', !peer.verified);
  elements.selectedPresence.classList.toggle('online', Boolean(peer.online));
  elements.selectedStatus.textContent = peer.online
    ? 'Online'
    : `Last seen ${formatRelative(peer.last_seen)}`;
}

function renderConversation({ preserveScroll = false } = {}) {
  const peer = state.peers.get(state.selectedId);
  if (!peer) {
    elements.emptyState.classList.remove('hidden');
    elements.conversation.classList.add('hidden');
    return;
  }
  elements.emptyState.classList.add('hidden');
  elements.conversation.classList.remove('hidden');
  renderConversationHeader();

  const container = elements.messages;
  const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
  const previousHeight = container.scrollHeight;
  const previousTop = container.scrollTop;

  const entry = thread(peer.node_id);
  const nodes = [];
  if (entry.loaded && entry.hasMore) {
    nodes.push(el('div', { className: 'history-hint', text: entry.loading ? 'Loading earlier messages…' : 'Scroll up for earlier messages' }));
  }
  if (entry.loaded && !entry.messages.length) {
    nodes.push(
      el('div', { className: 'conversation-placeholder' }, [
        el('strong', { text: `Say hello to ${displayName(peer)}` }),
        el('span', { text: 'Messages and files are end-to-end encrypted. Drop files here to share them.' }),
      ]),
    );
  }
  let previous = null;
  for (const message of entry.messages) {
    if (!previous || formatDay(previous.sent_at) !== formatDay(message.sent_at)) {
      nodes.push(el('div', { className: 'day-divider' }, [el('span', { text: formatDay(message.sent_at) })]));
      previous = null;
    }
    const grouped = Boolean(previous)
      && previous.direction === message.direction
      && message.sent_at - previous.sent_at < GROUP_WINDOW_SECONDS;
    nodes.push(renderMessageRow(message, grouped));
    previous = message;
  }
  container.replaceChildren(...nodes);

  if (preserveScroll === 'prepend') {
    container.scrollTop = previousTop + (container.scrollHeight - previousHeight);
  } else if (!preserveScroll || distanceFromBottom < 120) {
    container.scrollTop = container.scrollHeight;
  }
}

function renderMessageRow(message, grouped) {
  const row = el('div', {
    className: `message-row ${message.direction}${grouped ? ' grouped' : ''}`,
    dataset: { messageId: message.message_id },
  });
  if (message.kind === 'file' && message.transfer) {
    row.append(renderAttachment(message));
  } else {
    row.append(el('div', { className: 'message-bubble' }, [linkify(message.text)]));
  }
  row.append(renderMeta(message));
  return row;
}

function replaceMessageRow(message) {
  const existing = elements.messages.querySelector(`[data-message-id="${CSS.escape(message.message_id)}"]`);
  if (!existing) return;
  existing.replaceWith(renderMessageRow(message, existing.classList.contains('grouped')));
}

const STATUS_DISPLAY = {
  pending: ['clock', 'Sending'],
  queued: ['relay', 'Queued at relay until the recipient is online'],
  sent: ['check', 'Sent'],
  delivered: ['checks', 'Delivered'],
  read: ['checks', 'Read'],
  failed: ['alert', 'Not delivered'],
};

function renderMeta(message) {
  const meta = el('div', { className: 'message-meta' }, [el('span', { text: formatTime(message.sent_at) })]);
  if (message.direction === 'incoming') {
    if (message.mode === 'relay') meta.append(el('span', { className: 'meta-route', text: 'relayed' }));
    return meta;
  }
  const [iconName, label] = STATUS_DISPLAY[message.status] || STATUS_DISPLAY.pending;
  meta.append(el('span', { className: `status-icon status-${message.status}`, title: label, attrs: { 'aria-label': label } }, [icon(iconName)]));
  if (message.status === 'failed') {
    meta.classList.add('failed');
    meta.append(
      el('button', {
        className: 'link-button',
        type: 'button',
        text: 'Retry',
        onClick: () => retryMessage(message),
      }),
    );
  }
  return meta;
}

function transferStatusText(message) {
  const transfer = message.transfer;
  const peer = state.peers.get(message.peer_id);
  const percent = transfer.size ? Math.min(100, Math.floor((transfer.bytes_done / transfer.size) * 100)) : 0;
  switch (transfer.status) {
    case 'waiting':
      return `Waiting for ${displayName(peer)} to come online`;
    case 'sending':
      return `Sending · ${percent}%`;
    case 'receiving':
      return `Receiving · ${percent}%`;
    case 'done':
      return formatBytes(transfer.size);
    case 'cancelled':
      return 'Cancelled';
    default:
      return transfer.error ? `Failed · ${transfer.error}` : 'Failed';
  }
}

function renderAttachment(message) {
  const transfer = message.transfer;
  const available = transfer.status === 'done' || transfer.direction === 'outgoing';
  const active = ACTIVE_TRANSFER.has(transfer.status);
  const card = el('div', { className: `attachment ${transfer.status}` });

  if (isPreviewableImage(transfer) && available && transfer.status !== 'cancelled') {
    const image = el('img', {
      className: 'attachment-image',
      attrs: { src: mediaUrl(transfer.path), alt: transfer.name, loading: 'lazy', draggable: 'false' },
    });
    image.addEventListener('error', () => image.classList.add('broken'));
    const frame = el('button', {
      className: 'image-frame',
      type: 'button',
      title: transfer.name,
      onClick: () => openLightbox(transfer),
    }, [image]);
    card.classList.add('has-image');
    card.append(frame);
  }

  const details = el('div', { className: 'attachment-row' }, [
    el('div', { className: 'file-badge', text: fileExtension(transfer.name) }),
    el('div', { className: 'file-info' }, [
      el('strong', { text: transfer.name, title: transfer.name }),
      el('span', { text: transferStatusText(message) }),
    ]),
  ]);
  const actions = el('div', { className: 'file-actions' });
  if (active) {
    actions.append(iconButton('close', 'Cancel transfer', () => cancelTransfer(transfer)));
  } else if (transfer.status === 'failed' && transfer.direction === 'outgoing') {
    actions.append(iconButton('retry', 'Try again', () => retryMessage(message)));
  }
  if (available && transfer.status !== 'cancelled' && transfer.status !== 'failed') {
    actions.append(
      iconButton('open', 'Open', () => fileAction(window.cnp2p.openFile, transfer.path)),
      iconButton('download', 'Save as…', () => fileAction(window.cnp2p.saveFileAs, transfer.path)),
      iconButton('folder', 'Show in folder', () => fileAction(window.cnp2p.revealFile, transfer.path)),
    );
  }
  details.append(actions);
  card.append(details);

  if (active && transfer.status !== 'waiting') {
    const bar = el('div', { className: 'progress-bar' });
    const fill = el('div', { className: 'progress-fill' });
    fill.style.width = `${transfer.size ? (transfer.bytes_done / transfer.size) * 100 : 0}%`;
    bar.append(fill);
    card.append(bar);
  }
  return card;
}

async function fileAction(action, filePath) {
  try {
    await action(filePath);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

async function cancelTransfer(transfer) {
  try {
    await window.cnp2p.request('cancel_transfer', { transfer_id: transfer.transfer_id });
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function openLightbox(transfer) {
  state.lightboxPath = transfer.path;
  elements.lightboxImage.src = mediaUrl(transfer.path);
  elements.lightboxImage.alt = transfer.name;
  elements.lightboxName.textContent = `${transfer.name} · ${formatBytes(transfer.size)}`;
  elements.lightbox.classList.remove('hidden');
  $('#lightbox-close').focus();
}

function closeLightbox() {
  elements.lightbox.classList.add('hidden');
  elements.lightboxImage.removeAttribute('src');
  state.lightboxPath = null;
}

async function loadHistory(peerId, { older = false } = {}) {
  const entry = thread(peerId);
  if (entry.loading || (older && !entry.hasMore)) return;
  entry.loading = true;
  try {
    const before = older && entry.messages.length ? entry.messages[0].sent_at : undefined;
    const result = await window.cnp2p.request('history', { node_id: peerId, before, limit: 60 });
    const known = new Set(entry.messages.map((message) => message.message_id));
    const fetched = result.messages.filter((message) => !known.has(message.message_id)).map(mergeKnown);
    entry.messages = older ? [...fetched, ...entry.messages] : mergeThread(entry.messages, fetched);
    entry.hasMore = result.messages.length >= 60;
    entry.loaded = true;
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    entry.loading = false;
  }
  if (state.selectedId === peerId) renderConversation({ preserveScroll: older ? 'prepend' : false });
}

function mergeThread(current, fetched) {
  const byId = new Map(current.map((message) => [message.message_id, message]));
  for (const message of fetched) byId.set(message.message_id, message);
  return [...byId.values()].sort((a, b) => a.sent_at - b.sent_at);
}

function addMessageToThread(message) {
  const entry = thread(message.peer_id);
  if (!entry.loaded) return false;
  if (entry.messages.some((existing) => existing.message_id === message.message_id)) return false;
  entry.messages.push(mergeKnown(message));
  entry.messages.sort((a, b) => a.sent_at - b.sent_at);
  return true;
}

elements.messages.addEventListener('scroll', () => {
  if (elements.messages.scrollTop < 80 && state.selectedId) {
    const entry = thread(state.selectedId);
    if (entry.loaded && entry.hasMore && !entry.loading) void loadHistory(state.selectedId, { older: true });
  }
});

$('#lightbox-close').addEventListener('click', closeLightbox);
elements.lightbox.addEventListener('click', (event) => {
  if (event.target === elements.lightbox) closeLightbox();
});
$('#lightbox-save').addEventListener('click', () => fileAction(window.cnp2p.saveFileAs, state.lightboxPath));
$('#lightbox-reveal').addEventListener('click', () => fileAction(window.cnp2p.revealFile, state.lightboxPath));
