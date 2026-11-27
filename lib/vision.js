// Which image model answers shisei.py's picture questions (pure functions; lib/host.js uses them):
// - 'dsh': dsh's default model, when it reads images (nothing more to set up)
// - 'key': the separate image model (shisei config vision_base_url / vision_model, its key in the dsh credential store)
// shisei.py sends one OpenAI-style chat (text + data: images); for 'dsh' it is turned into dsh message parts here.

/** What the user picked on the page: 'dsh' / 'key'; never picked is 'auto' (dsh when it can). */
export function visionPref(config) {
  const v = config?.vision_source
  return v === 'dsh' || v === 'key' ? v : 'auto'
}

/**
 * The route for this call, or null (none usable).
 * director.verified: the fixed test picture through dsh's model passed (true), failed (false) or was never tried. A model
 * dsh declares image-capable may still be refused by its provider (deepseek-flash answered 404, 2026-11-13), so 'auto'
 * prefers a saved separate key until dsh's model has passed the test, and never uses one that failed it. Picking 'dsh'
 * by hand always tries it (that is how it gets tested again).
 * @param {{ pref: 'auto'|'dsh'|'key', director: { image: boolean|null, verified?: boolean } | null, hasKey: boolean, canStream: boolean }} s
 */
export function pickVisionSource({ pref, director, hasKey, canStream }) {
  const dsh = canStream && director?.image === true
  if (pref === 'key') return hasKey ? 'key' : null
  if (pref === 'dsh') return dsh ? 'dsh' : null
  if (dsh && director.verified === true) return 'dsh'
  if (hasKey) return 'key'
  return dsh && director.verified !== false ? 'dsh' : null
}

/** Key for the remembered test result of dsh's model. */
export const verifiedKey = (d) => (d ? `${d.provider}/${d.model}` : '')

const DATA_URL = /^data:(image\/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)$/u

/**
 * shisei.py's OpenAI-style messages → the parts of one dsh user message, in order. Only one user message with text and
 * data: images is accepted; anything else (http image URLs, several messages) is refused, so the host never fetches URLs.
 * @returns {Array<{ type: 'text', text: string } | { type: 'image', data: Uint8Array, mediaType: string }>}
 */
export function openaiToParts(messages) {
  if (!Array.isArray(messages) || messages.length !== 1 || messages[0]?.role !== 'user') throw new Error('one user message expected')
  const content = typeof messages[0].content === 'string' ? [{ type: 'text', text: messages[0].content }] : messages[0].content
  if (!Array.isArray(content) || !content.length) throw new Error('empty message')
  return content.map((c) => {
    if (c?.type === 'text' && typeof c.text === 'string') return { type: 'text', text: c.text }
    if (c?.type === 'image_url') {
      const m = DATA_URL.exec(String(c.image_url?.url ?? ''))
      if (!m) throw new Error('only data: images are accepted')
      return { type: 'image', mediaType: m[1], data: new Uint8Array(Buffer.from(m[2], 'base64')) }
    }
    throw new Error(`unsupported content part: ${c?.type}`)
  })
}

/** dsh's StreamChunks → an OpenAI-style reply (shisei.py reads only choices[0].message.content). Throws on failure. */
export async function collectReply(stream, model) {
  let text = ''
  let finish = null
  for await (const ch of stream) {
    if (ch?.type === 'text-delta') text += ch.text
    else if (ch?.type === 'finish') finish = ch.reason
  }
  const kind = finish?.kind
  if (kind === 'error' || kind === 'aborted') throw new Error(finish.failure?.message || `model ${kind}`)
  if (!text.trim()) throw new Error(kind === 'max-tokens' ? 'the model used up its tokens before answering' : 'the model gave no answer')
  return { model, choices: [{ message: { role: 'assistant', content: text }, finish_reason: kind ?? 'stop' }] }
}
