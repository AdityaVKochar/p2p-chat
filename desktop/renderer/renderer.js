const elements = {
  statusDot: document.querySelector('#status-dot'),
  statusLabel: document.querySelector('#status-label'),
  peerCount: document.querySelector('#peer-count'),
  peerList: document.querySelector('#peer-list'),
  noPeers: document.querySelector('#no-peers'),
  selfAvatar: document.querySelector('#self-avatar'),
  selfName: document.querySelector('#self-name'),
  copyId: document.querySelector('#copy-id'),
  emptyState: document.querySelector('#empty-state'),
  conversation: document.querySelector('#conversation'),
  selectedAvatar: document.querySelector('#selected-avatar'),
  selectedName: document.querySelector('#selected-name'),
  selectedEndpoint: document.querySelector('#selected-endpoint'),
  messages: document.querySelector('#messages'),
  messageInput: document.querySelector('#message-input'),
  sendButton: document.querySelector('#send-button'),
  settingsPanel: document.querySelector('#settings-panel'),
  scrim: document.querySelector('#scrim'),
  onboarding: document.querySelector('#onboarding'),
  onboardingName: document.querySelector('#onboarding-name'),
  toastRegion: document.querySelector('#toast-region'),
};

const state = {
  running: false,
  settings: null,
  self: null,
  peers: new Map(),
  messages: new Map(),
  selectedId: null,
};

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  return parts.slice(0, 2).map((part) => part[0]).join('').toUpperCase() || '?';
}

function showToast(text, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = text;
  elements.toastRegion.append(toast);
  setTimeout(() => toast.remove(), 4_500);
}

function setConnection(stateName, label) {
  elements.statusDot.className = `status-dot ${stateName}`;
  elements.statusLabel.textContent = label;
}

function formatTime(seconds) {
  const date = new Date(Number(seconds) * 1000);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function setPeers(peers) {
  for (const peer of peers || []) state.peers.set(peer.node_id, peer);
  renderPeers();
  if (state.selectedId) renderConversation();
}

function renderPeers() {
  elements.peerList.replaceChildren();
  const peers = [...state.peers.values()].sort((a, b) => a.name.localeCompare(b.name));
  elements.peerCount.textContent = `${peers.length} ${peers.length === 1 ? 'peer' : 'peers'}`;
  elements.noPeers.classList.toggle('hidden', peers.length > 0);
  for (const peer of peers) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `peer-item${peer.node_id === state.selectedId ? ' active' : ''}`;
    button.dataset.nodeId = peer.node_id;

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = initials(peer.name);
    const info = document.createElement('div');
    info.className = 'peer-info';
    const name = document.createElement('strong');
    name.textContent = peer.name;
    const id = document.createElement('span');
    id.textContent = peer.node_id.slice(0, 12);
    info.append(name, id);
    button.append(avatar, info);
    if (peer.relay) {
      const relay = document.createElement('span');
      relay.className = 'relay-badge';
      relay.textContent = 'relay';
      button.append(relay);
    }
    button.addEventListener('click', () => selectPeer(peer.node_id));
    elements.peerList.append(button);
  }
}

function selectPeer(nodeId) {
  state.selectedId = nodeId;
  renderPeers();
  renderConversation();
  elements.messageInput.focus();
}

function conversationMessages(nodeId) {
  if (!state.messages.has(nodeId)) state.messages.set(nodeId, []);
  return state.messages.get(nodeId);
}

function renderConversation() {
  const peer = state.peers.get(state.selectedId);
  if (!peer) {
    elements.emptyState.classList.remove('hidden');
    elements.conversation.classList.add('hidden');
    return;
  }
  elements.emptyState.classList.add('hidden');
  elements.conversation.classList.remove('hidden');
  elements.selectedAvatar.textContent = initials(peer.name);
  elements.selectedName.textContent = peer.name;
  elements.selectedEndpoint.textContent = `${peer.host}:${peer.port}`;
  elements.messages.replaceChildren();
  const messages = conversationMessages(peer.node_id);
  if (!messages.length) {
    const placeholder = document.createElement('p');
    placeholder.className = 'conversation-placeholder';
    placeholder.textContent = 'Messages are end-to-end encrypted.';
    elements.messages.append(placeholder);
    return;
  }
  for (const message of messages) {
    const row = document.createElement('div');
    row.className = `message-row ${message.direction}`;
    const bubble = document.createElement('div');
    bubble.className = 'message-bubble';
    bubble.textContent = message.text;
    const meta = document.createElement('div');
    meta.className = `message-meta${message.failed ? ' failed' : ''}`;
    const route = message.offline ? ' · relayed' : '';
    const delivery = message.pending ? ' · sending' : message.failed ? ' · failed' : route;
    meta.textContent = `${formatTime(message.sent_at)}${delivery}`;
    row.append(bubble, meta);
    elements.messages.append(row);
  }
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function applySnapshot(snapshot) {
  state.running = Boolean(snapshot.running);
  if (!snapshot.running) {
    setConnection('offline', 'Offline');
    return;
  }
  state.self = snapshot.self;
  setConnection('online', snapshot.upnp ? 'Online · mapped' : 'Online');
  elements.selfAvatar.textContent = initials(snapshot.self.name);
  elements.selfName.textContent = snapshot.self.name;
  elements.copyId.disabled = false;
  setPeers(snapshot.peers);
}

function settingsPayload() {
  const bootstrap = state.settings.bootstrap ? [state.settings.bootstrap] : [];
  const relays = state.settings.relay ? [state.settings.relay] : [];
  return {
    name: state.settings.name,
    port: state.settings.port,
    bootstrap,
    relays,
    stun: state.settings.stun,
    lan_discovery: state.settings.lanDiscovery,
    upnp: state.settings.upnp,
  };
}

async function startNode() {
  setConnection('connecting', 'Starting…');
  try {
    const snapshot = await window.cnp2p.request('start', settingsPayload());
    state.peers.clear();
    applySnapshot(snapshot);
  } catch (error) {
    setConnection('offline', 'Offline');
    showToast(error.message, 'error');
    openSettings();
  }
}

function addIncomingMessage(data) {
  state.peers.set(data.sender.node_id, data.sender);
  conversationMessages(data.sender.node_id).push({
    direction: 'incoming',
    text: data.text,
    sent_at: data.sent_at,
    offline: data.offline,
  });
  renderPeers();
  if (state.selectedId === data.sender.node_id) renderConversation();
  else showToast(`New encrypted message from ${data.sender.name}`);
}

function openSettings() {
  populateSettings();
  elements.settingsPanel.classList.add('open');
  elements.settingsPanel.setAttribute('aria-hidden', 'false');
  elements.scrim.classList.remove('hidden');
}

function closeSettings() {
  elements.settingsPanel.classList.remove('open');
  elements.settingsPanel.setAttribute('aria-hidden', 'true');
  elements.scrim.classList.add('hidden');
}

function populateSettings() {
  document.querySelector('#setting-name').value = state.settings.name;
  document.querySelector('#setting-port').value = state.settings.port;
  document.querySelector('#setting-bootstrap').value = state.settings.bootstrap;
  document.querySelector('#setting-relay').value = state.settings.relay;
  document.querySelector('#setting-stun').value = state.settings.stun;
  document.querySelector('#setting-lan').checked = state.settings.lanDiscovery;
  document.querySelector('#setting-upnp').checked = state.settings.upnp;
}

function readSettingsForm() {
  return {
    name: document.querySelector('#setting-name').value.trim(),
    port: Number(document.querySelector('#setting-port').value),
    bootstrap: document.querySelector('#setting-bootstrap').value.trim(),
    relay: document.querySelector('#setting-relay').value.trim(),
    stun: document.querySelector('#setting-stun').value.trim(),
    lanDiscovery: document.querySelector('#setting-lan').checked,
    upnp: document.querySelector('#setting-upnp').checked,
  };
}

document.querySelector('#settings-button').addEventListener('click', openSettings);
document.querySelector('#close-settings').addEventListener('click', closeSettings);
elements.scrim.addEventListener('click', closeSettings);

document.querySelector('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.querySelector('#save-settings');
  button.disabled = true;
  try {
    state.settings = await window.cnp2p.saveSettings(readSettingsForm());
    closeSettings();
    await startNode();
  } finally {
    button.disabled = false;
  }
});

document.querySelector('#onboarding-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  state.settings.name = elements.onboardingName.value.trim();
  state.settings = await window.cnp2p.saveSettings(state.settings);
  elements.onboarding.classList.add('hidden');
  await startNode();
});

document.querySelector('#connect-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.querySelector('#connect-address');
  const address = input.value.trim();
  if (!address) return;
  try {
    const result = await window.cnp2p.request('connect', { address });
    input.value = '';
    applySnapshot(result.snapshot);
    selectPeer(result.peer.node_id);
    showToast(`Connected to ${result.peer.name}`);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

document.querySelector('#message-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = elements.messageInput.value.trim();
  const peer = state.peers.get(state.selectedId);
  if (!text || !peer) return;
  elements.messageInput.value = '';
  elements.messageInput.style.height = '';
  const message = {
    direction: 'outgoing',
    text,
    sent_at: Date.now() / 1000,
    pending: true,
  };
  conversationMessages(peer.node_id).push(message);
  renderConversation();
  try {
    const receipt = await window.cnp2p.request('send', { node_id: peer.node_id, text });
    message.pending = false;
    message.offline = receipt.mode === 'relay-queued';
  } catch (error) {
    message.pending = false;
    message.failed = true;
    showToast(error.message, 'error');
  }
  renderConversation();
});

elements.messageInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    document.querySelector('#message-form').requestSubmit();
  }
});

elements.messageInput.addEventListener('input', () => {
  elements.messageInput.style.height = 'auto';
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 120)}px`;
});

document.querySelector('#ping-button').addEventListener('click', async () => {
  if (!state.selectedId) return;
  try {
    const result = await window.cnp2p.request('ping', { node_id: state.selectedId });
    showToast(`Peer replied in ${result.milliseconds.toFixed(1)} ms`);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

document.querySelector('#check-nat').addEventListener('click', async () => {
  try {
    state.settings = await window.cnp2p.saveSettings(readSettingsForm());
    await startNode();
    const endpoint = await window.cnp2p.request('nat', {});
    showToast(`Public UDP endpoint: ${endpoint.host}:${endpoint.port}`);
  } catch (error) {
    showToast(error.message, 'error');
  }
});

elements.copyId.addEventListener('click', async () => {
  if (!state.self) return;
  await navigator.clipboard.writeText(state.self.node_id);
  showToast('Node ID copied');
});

window.cnp2p.onEvent((message) => {
  if (message.event === 'peer') {
    state.peers.set(message.data.node_id, message.data);
    renderPeers();
  } else if (message.event === 'message') {
    addIncomingMessage(message.data);
  } else if (message.event === 'status') {
    const status = message.data;
    if (status.category === 'node' && status.state === 'offline') {
      setConnection('offline', 'Offline');
    } else if (status.state === 'warning') {
      showToast(status.detail, 'error');
    } else if (status.category === 'nat' && status.state === 'unavailable') {
      setConnection('online', 'Online');
    } else if (status.category === 'nat' && status.state === 'mapped') {
      setConnection('online', 'Online · mapped');
    }
  }
});

setInterval(async () => {
  if (!state.running) return;
  try {
    applySnapshot(await window.cnp2p.request('snapshot', {}));
  } catch {
    setConnection('offline', 'Offline');
  }
}, 5_000);

async function initialize() {
  state.settings = await window.cnp2p.loadSettings();
  if (!state.settings.name) {
    elements.onboarding.classList.remove('hidden');
    elements.onboardingName.focus();
    return;
  }
  await startNode();
}

void initialize();
