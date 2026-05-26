/** Current chat session id, kept in memory across route changes.
 *
 *  ChatPage stores sessionId in local useState, which gets lost when the
 *  user navigates away (e.g. clicks an `entry:` citation that opens the
 *  Library) and comes back. The server-side session stays open — only the
 *  GUI's "currently selected" pointer needs to survive the round-trip.
 *
 *  Deliberately not persisted to localStorage: on next app launch we want
 *  the chat to start clean rather than auto-reload yesterday's session.
 *  Click a row in SessionList to resume any older session.
 */
import { create } from "zustand";

interface ChatSessionState {
  sessionId: string | null;
  setSessionId: (id: string | null) => void;
}

export const useChatSession = create<ChatSessionState>((set) => ({
  sessionId: null,
  setSessionId: (id) => set({ sessionId: id }),
}));
