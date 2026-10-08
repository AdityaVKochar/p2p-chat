// Application controller: sidebar, events from the networking engine,
// sending, settings, onboarding, and keyboard shortcuts.

const MAX_MESSAGE_CHARACTERS = 4096;
let restartDelay = 1_000;
let restartTimer = null;
let sidebarFrame = 0;

// Connection state -----------------------------------------------------------

function setConnection(stateName, label) {
  elements.statusDot.className = `status-dot ${stateName}`;
  elements.statusLabel.textContent = label;
}

function connectionLabel(snapshot) {
  const online = [...state.peers.values()].filter((peer) => peer.online).length;
  const peers = `${online} ${online === 1 ? 'peer' : 'peers'} online`;
  return snapshot && snapshot.upnp ? `Online · mapped · ${peers}` : `Online · ${peers}`;
}

function applySnapshot(snapshot) {
  state.running = Boolean(snapshot.running);
  if (!snapshot.running) {
    setConnection('offline', 'Offline');
    return;
  }
  state.self = snapshot.self;
  state.invite = snapshot.invite || '';
  for (const peer of snapshot.peers || []) state.peers.set(peer.node_id, peer);
  setConnection('online', connectionLabel(snapshot));
  setAvatar(elements.selfAvatar, snapshot.self);
  elements.selfName.textContent = snapshot.self.name;
  elements.selfId.textContent = snapshot.self.node_id.slice(0, 16);
  elements.copyInvite.disabled = !state.invite;
  $('#modal-invite').textContent = state.invite || '—';
  $('#setting-invite').value = state.invite;
  setUnread(snapshot.unread);
  scheduleSidebar();
  if (state.selectedId) renderConversationHeader();
}

function setUnread(count) {
  const unread = Number(count) || 0;
  if (unread === state.unread) return;
  state.unread = unread;
  document.title = unread ? `(${unread}) CNP2P` : 'CNP2P';
  void window.cnp2p.setBadge(unread);
}

function settingsPayload() {
  return {
    name: state.settings.name,
    port: state.settings.port,
    bootstrap: state.settings.bootstrap ? [state.settings.bootstrap] : [],
    relays: state.settings.relay ? [state.settings.relay] : [],
    stun: state.settings.stun,
    lan_discovery: state.settings.lanDiscovery,
    upnp: state.settings.upnp,
    read_receipts: state.settings.readReceipts,
    max_file_mb: state.settings.maxFileMb,
  };
}

async function startNode() {
  clearTimeout(restartTimer);
  setConnection('connecting', 'Starting…');
  try {
    const snapshot = await window.cnp2p.request('start', settingsPayload());
    restartDelay = 1_000;
    applySnapshot(snapshot);
    await refreshConversations();
    if (state.selectedId) await loadHistory(state.selectedId);
    return true;
  } catch (error) {
    setConnection('offline', 'Offline');
    showToast(error.message, 'error');
    return false;
  }
}

// The networking service restarts with backoff if it stops unexpectedly.
function scheduleRestart() {
  state.running = false;
  setConnection('offline', `Reconnecting in ${Math.round(restartDelay / 1000)} s`);
  clearTimeout(restartTimer);
  restartTimer = setTimeout(async () => {
    if (!(await startNode())) {
      restartDelay = Math.min(restartDelay * 2, 30_000);
      scheduleRestart();
    }
  }, restartDelay);
}

async function refreshConversations() {
  try {
    const result = await window.cnp2p.request('conversations', {});
    state.conversations = new Map(result.conversations.map((entry) => [entry.peer_id, entry]));
    setUnread(result.unread);
    scheduleSidebar();
  } catch (error) {
    showToast(error.message, 'error');
  }
}

// Sidebar ----------------------------------------------------------------------

function scheduleSidebar() {
  if (sidebarFrame) return;
  sidebarFrame = requestAnimationFrame(() => {
    sidebarFrame = 0;
    renderSidebar();
  });
}

function matchesFilter(peer) {
  if (!state.filter) return true;
  const needle = state.filter.toLowerCase();
  return displayName(peer).toLowerCase().includes(needle)
    || String(peer.name).toLowerCase().includes(needle)
    || peer.node_id.startsWith(needle);
}

function previewText(message) {
  if (!message) return '';
  const prefix = message.direction === 'outgoing' ? 'You: ' : '';
  if (message.kind === 'file') {
    const kind = message.transfer && isPreviewableImage(message.transfer) ? 'Photo' : 'File';
    return `${prefix}${kind} · ${message.text}`;
  }
  return `${prefix}${message.text.replace(/\s+/g, ' ')}`;
}

function peerButton(peer, conversation) {
  const avatar = el('div', { className: 'avatar' });
  setAvatar(avatar, peer);
  const presence = el('span', { className: `presence-dot${peer.online ? ' online' : ''}` });
  const top = el('div', { className: 'peer-line' }, [
    el('strong', { text: displayName(peer) }),
    conversation ? el('time', { text: formatListTime(conversation.last_activity) }) : null,
  ]);
  const bottomText = conversation
    ? previewText(conversation.last_message)
    : `${peer.node_id.slice(0, 12)}${peer.relay ? ' · relay' : ''}`;
  const bottom = el('div', { className: 'peer-line' }, [
    el('span', { className: conversation ? 'preview' : 'mono', text: bottomText }),
    conversation && conversation.unread ? el('span', { className: 'unread-badge', text: String(conversation.unread) }) : null,
  ]);
  const button = el('button', {
    className: `peer-item${peer.node_id === state.selectedId ? ' active' : ''}${conversation && conversation.unread ? ' unread' : ''}`,
    type: 'button',
    attrs: { role: 'listitem' },
    dataset: { nodeId: peer.node_id },
    onClick: () => selectPeer(peer.node_id),
  }, [
    el('div', { className: 'avatar-wrap' }, [avatar, presence]),
    el('div', { className: 'peer-info' }, [top, bottom]),
  ]);
  return button;
}

function renderSidebar() {
  const conversations = [...state.conversations.values()]
    .sort((a, b) => b.last_activity - a.last_activity)
    .filter((entry) => state.peers.has(entry.peer_id) && matchesFilter(state.peers.get(entry.peer_id)));
  const others = [...state.peers.values()]
    .filter((peer) => !state.conversations.has(peer.node_id) && matchesFilter(peer))
    .sort((a, b) => Number(b.online) - Number(a.online) || displayName(a).localeCompare(displayName(b)));

  elements.conversationList.replaceChildren(
    ...conversations.map((entry) => peerButton(state.peers.get(entry.peer_id), entry)),
  );
  elements.peerList.replaceChildren(...others.map((peer) => peerButton(peer, null)));
  elements.chatsLabel.classList.toggle('hidden', !conversations.length);
  elements.peersLabel.classList.toggle('hidden', !others.length);
  const nothing = !conversations.length && !others.length;
  elements.noPeers.classList.toggle('hidden', !nothing);
  elements.noPeers.querySelector('p').textContent = state.filter ? 'No matches' : 'No peers yet';
  elements.noPeers.querySelector('span').classList.toggle('hidden', Boolean(state.filter));
  $('#empty-add-contact').classList.toggle('hidden', Boolean(state.filter));
}

async function selectPeer(nodeId) {
  if (!state.peers.has(nodeId)) return;
  state.selectedId = nodeId;
  closeDrawers();
  scheduleSidebar();
  const entry = thread(nodeId);
  renderConversation();
  elements.messageInput.focus();
  if (!entry.loaded) await loadHistory(nodeId);
  markSelectedRead();
}

function markSelectedRead() {
  const peerId = state.selectedId;
  const conversation = peerId && state.conversations.get(peerId);
  if (!conversation || !conversation.unread || !document.hasFocus()) return;
  conversation.unread = 0;
  scheduleSidebar();
  window.cnp2p.request('mark_read', { node_id: peerId })
    .then((result) => setUnread(result.unread))
    .catch(() => {});
}

// Sending ------------------------------------------------------------------------

function touchConversation(message) {
  const existing = state.conversations.get(message.peer_id);
  const unread = (existing ? existing.unread : 0)
    + (message.direction === 'incoming' && message.status === 'received' ? 1 : 0);
  if (existing && existing.last_message && existing.last_message.sent_at > message.sent_at) {
    existing.unread = unread;
  } else {
    state.conversations.set(message.peer_id, {
      peer_id: message.peer_id,
      last_activity: message.sent_at,
      unread,
      last_message: message,
    });
  }
  scheduleSidebar();
}

async function sendText(text) {
  const peerId = state.selectedId;
  const message = {
    message_id: newMessageId(),
    peer_id: peerId,
    direction: 'outgoing',
    kind: 'text',
    text,
    sent_at: Date.now() / 1000,
    status: 'pending',
    mode: null,
    transfer: null,
  };
  thread(peerId).messages.push(message);
  touchConversation(message);
  renderConversation();
  try {
    const receipt = await window.cnp2p.request('send', { node_id: peerId, text, message_id: message.message_id });
    applyStatus(message, receipt.mode === 'direct' ? 'delivered' : 'queued', { upgradeOnly: true });
  } catch (error) {
    applyStatus(message, 'failed');
    showToast(error.message, 'error');
  }
}

async function retryMessage(message) {
  applyStatus(message, 'pending');
  try {
    const result = await window.cnp2p.request('retry', { message_id: message.message_id });
    if (result.mode !== 'file') applyStatus(message, result.mode === 'direct' ? 'delivered' : 'queued', { upgradeOnly: true });
  } catch (error) {
    applyStatus(message, 'failed');
    showToast(error.message, 'error');
  }
}

function applyStatus(message, status, { upgradeOnly = false } = {}) {
  if (upgradeOnly && (STATUS_RANK[message.status] ?? 0) >= (STATUS_RANK[status] ?? 0)) return;
  message.status = status;
  if (message.peer_id === state.selectedId) replaceMessageRow(message);
  const conversation = state.conversations.get(message.peer_id);
  if (conversation && conversation.last_message && conversation.last_message.message_id === message.message_id) {
    conversation.last_message.status = status;
  }
}

async function sendFiles(paths, peerId = state.selectedId) {
  if (!peerId) return;
  for (const filePath of paths.filter(Boolean)) {
    try {
      const result = await window.cnp2p.request('send_file', { node_id: peerId, path: filePath });
      acceptOutgoingFile(result.message);
    } catch (error) {
      showToast(error.message, 'error');
    }
  }
}

function acceptOutgoingFile(message) {
  if (addMessageToThread(message) && message.peer_id === state.selectedId) renderConversation();
  touchConversation(message);
}

async function sendClipboardImages(files) {
  const peerId = state.selectedId;
  for (const file of files) {
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const result = await window.cnp2p.sendClipboardImage(peerId, bytes, file.type);
      acceptOutgoingFile(result.message);
    } catch (error) {
      showToast(error.message, 'error');
    }
  }
}

// Events from the networking engine ---------------------------------------------

function handleIncomingMessage(data) {
  const sender = data.sender;
  const existing = state.peers.get(sender.node_id);
  state.peers.set(sender.node_id, { ...existing, ...sender, online: true });
  const message = { ...data };
  delete message.sender;
  delete message.offline;
  delete message.encrypted;
  const visible = state.selectedId === sender.node_id && document.hasFocus();
  if (addMessageToThread(message) && state.selectedId === sender.node_id) renderConversation({ preserveScroll: true });
  touchConversation(message);
  setUnread(state.unread + 1);
  if (visible) {
    markSelectedRead();
  } else if (state.settings.notifications) {
    const body = message.kind === 'file' ? `Sent you a file: ${message.text}` : message.text;
    void window.cnp2p.notify({ title: displayName(state.peers.get(sender.node_id)), body, peerId: sender.node_id });
  }
  if (!visible && state.selectedId !== sender.node_id) {
    showToast(`New message from ${displayName(state.peers.get(sender.node_id))}`);
  }
}

function handleStatusUpdate(data) {
  for (const messageId of data.message_ids) {
    state.statuses.set(messageId, data.status);
    const message = findMessage(messageId);
    if (message) applyStatus(message, data.status);
    const conversation = state.conversations.get(data.peer_id);
    if (conversation && conversation.last_message && conversation.last_message.message_id === messageId) {
      conversation.last_message.status = data.status;
    }
  }
}

function handleTransferUpdate(transfer) {
  state.transfers.set(transfer.transfer_id, transfer);
  const message = findMessage(transfer.message_id);
  if (message) {
    message.transfer = transfer;
    if (message.peer_id === state.selectedId) replaceMessageRow(message);
  }
  const conversation = state.conversations.get(transfer.peer_id);
  if (conversation && conversation.last_message && conversation.last_message.message_id === transfer.message_id) {
    conversation.last_message.transfer = transfer;
  }
  if (transfer.status === 'done' && transfer.direction === 'incoming' && state.selectedId !== transfer.peer_id) {
    showToast(`Received ${transfer.name}`);
  }
}

window.cnp2p.onEvent((message) => {
  const { event, data } = message;
  if (event === 'peer') {
    state.peers.set(data.node_id, { ...state.peers.get(data.node_id), ...data });
    scheduleSidebar();
  } else if (event === 'message') {
    handleIncomingMessage(data);
  } else if (event === 'message_status') {
    handleStatusUpdate(data);
  } else if (event === 'transfer') {
    handleTransferUpdate(data.transfer);
  } else if (event === 'status') {
    if (data.category === 'node' && data.state === 'offline') {
      if (state.running) scheduleRestart();
      else setConnection('offline', 'Offline');
    } else if (data.state === 'warning') {
      showToast(data.detail, 'error');
    } else if (data.category === 'nat') {
      setConnection('online', connectionLabel({ upnp: data.state === 'mapped' }));
    }
  }
});

window.cnp2p.onSelectPeer((nodeId) => {
  if (nodeId) void selectPeer(nodeId);
});

setInterval(async () => {
  if (!state.running) return;
  try {
    applySnapshot(await window.cnp2p.request('snapshot', {}));
  } catch {
    setConnection('offline', 'Offline');
  }
}, 5_000);

window.addEventListener('focus', markSelectedRead);

// Overlays ---------------------------------------------------------------------

function openDrawer(panel) {
  closeDrawers();
  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
  elements.scrim.classList.remove('hidden');
}

function closeDrawers() {
  for (const panel of [elements.settingsPanel, elements.contactPanel]) {
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
  }
  elements.scrim.classList.add('hidden');
}

function openAddContact() {
  closeDrawers();
  elements.addContactModal.classList.remove('hidden');
  $('#connect-address').focus();
}

function closeAddContact() {
  elements.addContactModal.classList.add('hidden');
}

function closeTopOverlay() {
  if (!elements.lightbox.classList.contains('hidden')) closeLightbox();
  else if (!elements.addContactModal.classList.contains('hidden')) closeAddContact();
  else closeDrawers();
}

document.querySelectorAll('.close-drawer').forEach((button) => button.addEventListener('click', closeDrawers));
elements.scrim.addEventListener('click', closeDrawers);
$('#settings-button').addEventListener('click', () => {
  populateSettings();
  openDrawer(elements.settingsPanel);
});
for (const id of ['#add-contact-button', '#empty-add-contact', '#empty-add']) {
  $(id).addEventListener('click', openAddContact);
}
$('#cancel-connect').addEventListener('click', closeAddContact);
elements.addContactModal.addEventListener('click', (event) => {
  if (event.target === elements.addContactModal) closeAddContact();
});

for (const id of ['#copy-invite', '#empty-copy-invite', '#modal-copy-invite', '#settings-copy-invite', '#onboarding-copy']) {
  $(id).addEventListener('click', () => {
    if (!state.invite) {
      showToast('Your node is not online yet', 'error');
      return;
    }
    void copyText(state.invite, 'Invite code');
  });
}

$('#connect-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#connect-address');
  const address = input.value.trim();
  if (!address) return;
  const button = $('#submit-connect');
  button.disabled = true;
  button.textContent = 'Connecting…';
  try {
    const result = await window.cnp2p.request('connect', { address });
    input.value = '';
    applySnapshot(result.snapshot);
    closeAddContact();
    await selectPeer(result.peer.node_id);
    showToast(`Connected to ${displayName(result.peer)}`);
  } catch (error) {
    showToast(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Connect';
  }
});

// Contact details ------------------------------------------------------------------

async function openContact() {
  if (!state.selectedId) return;
  try {
    renderContact(await window.cnp2p.request('contact_info', { node_id: state.selectedId }));
    openDrawer(elements.contactPanel);
  } catch (error) {
    showToast(error.message, 'error');
  }
}

function renderContact(info) {
  const peer = info.peer;
  state.peers.set(peer.node_id, { ...state.peers.get(peer.node_id), ...peer });
  setAvatar($('#contact-avatar'), peer);
  $('#contact-name').textContent = displayName(peer);
  $('#contact-presence').textContent = peer.online ? 'Online' : `Last seen ${formatRelative(peer.last_seen)}`;
  $('#contact-nickname').value = peer.nickname;
  $('#contact-nickname').placeholder = `${peer.name} (only visible to you)`;
  $('#contact-verified').checked = peer.verified;
  const groups = info.safety_number.split(' ');
  $('#contact-safety').replaceChildren(
    ...groups.map((group) => el('span', { text: group })),
  );
  $('#contact-id').textContent = peer.node_id;
  $('#contact-endpoint').textContent = `${peer.host}:${peer.port}`;
  $('#contact-relay').textContent = peer.relay ? 'Offers encrypted relay service' : 'No';
  renderConversationHeader();
  scheduleSidebar();
}

$('#info-button').addEventListener('click', openContact);
$('#contact-save-nickname').addEventListener('click', async () => {
  try {
    renderContact(await window.cnp2p.request('rename_contact', {
      node_id: state.selectedId,
      nickname: $('#contact-nickname').value,
    }));
    showToast('Nickname saved');
  } catch (error) {
    showToast(error.message, 'error');
  }
});
$('#contact-nickname').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') $('#contact-save-nickname').click();
});
$('#contact-verified').addEventListener('change', async (event) => {
  try {
    renderContact(await window.cnp2p.request('set_verified', {
      node_id: state.selectedId,
      verified: event.target.checked,
    }));
  } catch (error) {
    showToast(error.message, 'error');
  }
});
$('#contact-copy-id').addEventListener('click', () => copyText(state.selectedId, 'Node ID'));
$('#contact-ping').addEventListener('click', async () => {
  try {
    const result = await window.cnp2p.request('ping', { node_id: state.selectedId });
    showToast(`Peer replied in ${result.milliseconds.toFixed(1)} ms`);
  } catch (error) {
    showToast(error.message, 'error');
  }
});
$('#contact-clear').addEventListener('click', async () => {
  const peerId = state.selectedId;
  const peer = state.peers.get(peerId);
  if (!window.confirm(`Delete the conversation with ${displayName(peer)} and all of its files from this device?`)) return;
  try {
    const result = await window.cnp2p.request('delete_conversation', { node_id: peerId });
    state.threads.delete(peerId);
    state.conversations.delete(peerId);
    setUnread(result.unread);
    closeDrawers();
    await loadHistory(peerId);
    scheduleSidebar();
  } catch (error) {
    showToast(error.message, 'error');
  }
});

// Composer -----------------------------------------------------------------------------

elements.messageForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = elements.messageInput.value.trim();
  if (!text || !state.peers.has(state.selectedId)) return;
  elements.messageInput.value = '';
  resizeComposer();
  void sendText(text);
});

function resizeComposer() {
  const input = elements.messageInput;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  const length = input.value.length;
  elements.charCounter.classList.toggle('hidden', length < MAX_MESSAGE_CHARACTERS - 500);
  elements.charCounter.textContent = `${length} / ${MAX_MESSAGE_CHARACTERS}`;
}

elements.messageInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    elements.messageForm.requestSubmit();
  }
});
elements.messageInput.addEventListener('input', resizeComposer);

elements.messageInput.addEventListener('paste', (event) => {
  const files = [...(event.clipboardData ? event.clipboardData.files : [])];
  if (!files.length) return;
  event.preventDefault();
  const withPaths = files.map((file) => window.cnp2p.pathForFile(file));
  if (withPaths.every(Boolean)) {
    void sendFiles(withPaths);
  } else {
    void sendClipboardImages(files.filter((file) => file.type.startsWith('image/')));
  }
});

async function pickAndSend() {
  if (!state.selectedId) return;
  const paths = await window.cnp2p.pickFiles();
  await sendFiles(paths);
}
$('#attach-button').addEventListener('click', pickAndSend);
$('#header-attach').addEventListener('click', pickAndSend);

// Drag and drop: highlight the conversation while files hover over it.
let dragDepth = 0;
function draggingFiles(event) {
  return event.dataTransfer && [...event.dataTransfer.types].includes('Files');
}
elements.chatPanel.addEventListener('dragenter', (event) => {
  if (!draggingFiles(event) || !state.selectedId) return;
  dragDepth += 1;
  elements.dropOverlay.classList.remove('hidden');
});
elements.chatPanel.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) elements.dropOverlay.classList.add('hidden');
});
elements.chatPanel.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  elements.dropOverlay.classList.add('hidden');
  if (!state.selectedId || !event.dataTransfer) return;
  const paths = [...event.dataTransfer.files].map((file) => window.cnp2p.pathForFile(file));
  void sendFiles(paths);
});
document.addEventListener('dragover', (event) => event.preventDefault());
document.addEventListener('drop', (event) => event.preventDefault());

// Search and keyboard -------------------------------------------------------------------------

elements.searchInput.addEventListener('input', () => {
  state.filter = elements.searchInput.value.trim();
  scheduleSidebar();
});
elements.searchInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    const first = document.querySelector('.sidebar .peer-item');
    if (first) void selectPeer(first.dataset.nodeId);
  }
});

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    elements.searchInput.focus();
    elements.searchInput.select();
  } else if (event.key === 'Escape') {
    if (document.activeElement === elements.searchInput && elements.searchInput.value) {
      elements.searchInput.value = '';
      state.filter = '';
      scheduleSidebar();
    } else {
      closeTopOverlay();
    }
  }
});

// Settings ----------------------------------------------------------------------------------

function populateSettings() {
  const settings = state.settings;
  $('#setting-name').value = settings.name;
  $('#setting-port').value = settings.port;
  $('#setting-bootstrap').value = settings.bootstrap;
  $('#setting-relay').value = settings.relay;
  $('#setting-stun').value = settings.stun;
  $('#setting-lan').checked = settings.lanDiscovery;
  $('#setting-upnp').checked = settings.upnp;
  $('#setting-receipts').checked = settings.readReceipts;
  $('#setting-notifications').checked = settings.notifications;
  $('#setting-max-file').value = settings.maxFileMb;
  $('#setting-invite').value = state.invite;
  for (const radio of document.querySelectorAll('input[name="theme"]')) radio.checked = radio.value === settings.theme;
}

function readSettingsForm() {
  const theme = document.querySelector('input[name="theme"]:checked');
  return {
    ...state.settings,
    name: $('#setting-name').value.trim(),
    port: Number($('#setting-port').value),
    bootstrap: $('#setting-bootstrap').value.trim(),
    relay: $('#setting-relay').value.trim(),
    stun: $('#setting-stun').value.trim(),
    lanDiscovery: $('#setting-lan').checked,
    upnp: $('#setting-upnp').checked,
    readReceipts: $('#setting-receipts').checked,
    notifications: $('#setting-notifications').checked,
    maxFileMb: Number($('#setting-max-file').value),
    theme: theme ? theme.value : 'system',
  };
}

// Appearance and notification changes apply immediately without a restart.
for (const input of document.querySelectorAll('input[name="theme"], #setting-notifications')) {
  input.addEventListener('change', async () => {
    const current = readSettingsForm();
    state.settings = await window.cnp2p.saveSettings({
      ...state.settings,
      theme: current.theme,
      notifications: current.notifications,
    });
  });
}

$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#save-settings');
  button.disabled = true;
  try {
    state.settings = await window.cnp2p.saveSettings(readSettingsForm());
    closeDrawers();
    await startNode();
  } finally {
    button.disabled = false;
  }
});

$('#check-nat').addEventListener('click', async () => {
  try {
    state.settings = await window.cnp2p.saveSettings(readSettingsForm());
    await startNode();
    const endpoint = await window.cnp2p.request('nat', {});
    showToast(`Public UDP endpoint: ${endpoint.host}:${endpoint.port}`);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

$('#open-attachments').addEventListener('click', () => fileAction(window.cnp2p.openAttachmentsFolder));

// Onboarding and startup ---------------------------------------------------------------------

$('#onboarding-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.settings = await window.cnp2p.saveSettings({ ...state.settings, name: elements.onboardingName.value.trim() });
  $('#onboarding-step-name').classList.add('hidden');
  $('#onboarding-step-invite').classList.remove('hidden');
  if (await startNode()) {
    $('#onboarding-invite').textContent = state.invite;
  } else {
    $('#onboarding-invite').textContent = 'The node could not start. Check Settings after continuing.';
  }
  $('#onboarding-done').focus();
});

$('#onboarding-done').addEventListener('click', () => {
  elements.onboarding.classList.add('hidden');
});

async function initialize() {
  state.settings = await window.cnp2p.loadSettings();
  if (!state.settings.name) {
    elements.onboarding.classList.remove('hidden');
    elements.onboardingName.focus();
    return;
  }
  if (!(await startNode())) {
    populateSettings();
    openDrawer(elements.settingsPanel);
  }
}

void initialize();
