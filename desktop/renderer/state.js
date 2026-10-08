// Shared renderer state. The networking engine owns the durable copy of
// conversations; this is a cache of what the window currently shows.

const $ = (selector) => document.querySelector(selector);

const elements = {
  statusDot: $('#status-dot'),
  statusLabel: $('#status-label'),
  searchInput: $('#search-input'),
  conversationList: $('#conversation-list'),
  chatsLabel: $('#chats-label'),
  peerList: $('#peer-list'),
  peersLabel: $('#peers-label'),
  noPeers: $('#no-peers'),
  selfAvatar: $('#self-avatar'),
  selfName: $('#self-name'),
  selfId: $('#self-id'),
  copyInvite: $('#copy-invite'),
  chatPanel: $('#chat-panel'),
  emptyState: $('#empty-state'),
  conversation: $('#conversation'),
  selectedAvatar: $('#selected-avatar'),
  selectedPresence: $('#selected-presence'),
  selectedName: $('#selected-name'),
  selectedVerified: $('#selected-verified'),
  selectedStatus: $('#selected-status'),
  messages: $('#messages'),
  messageForm: $('#message-form'),
  messageInput: $('#message-input'),
  charCounter: $('#char-counter'),
  dropOverlay: $('#drop-overlay'),
  scrim: $('#scrim'),
  settingsPanel: $('#settings-panel'),
  contactPanel: $('#contact-panel'),
  addContactModal: $('#add-contact-modal'),
  lightbox: $('#lightbox'),
  lightboxImage: $('#lightbox-image'),
  lightboxName: $('#lightbox-name'),
  onboarding: $('#onboarding'),
  onboardingName: $('#onboarding-name'),
};

const STATUS_RANK = { failed: 0, pending: 1, queued: 2, sent: 3, delivered: 4, received: 4, read: 5 };

const state = {
  running: false,
  settings: null,
  self: null,
  invite: '',
  peers: new Map(),
  conversations: new Map(),
  // peer_id -> { messages: [], loaded: bool, hasMore: bool, loading: bool }
  threads: new Map(),
  // Latest transfer records and message statuses seen in events, so that a
  // response that arrives after its own progress events never looks stale.
  transfers: new Map(),
  statuses: new Map(),
  selectedId: null,
  filter: '',
  unread: 0,
  lightboxPath: null,
};

function thread(peerId) {
  if (!state.threads.has(peerId)) {
    state.threads.set(peerId, { messages: [], loaded: false, hasMore: true, loading: false });
  }
  return state.threads.get(peerId);
}

function mergeKnown(message) {
  const status = state.statuses.get(message.message_id);
  if (status && (STATUS_RANK[status] ?? 0) > (STATUS_RANK[message.status] ?? 0)) message.status = status;
  if (message.transfer) {
    const latest = state.transfers.get(message.transfer.transfer_id);
    if (latest && latest.updated_at >= (message.transfer.updated_at || 0)) message.transfer = latest;
  }
  return message;
}

function findMessage(messageId) {
  for (const entry of state.threads.values()) {
    const found = entry.messages.find((message) => message.message_id === messageId);
    if (found) return found;
  }
  return null;
}
