/* Use the ordinary form path so attachments, validation and submission identity stay intact. */
'use strict';
globalThis.OIFComposer = (() => {
  const key = 'oif.sendKey';
  let mode = 'enter';
  try { if (localStorage.getItem(key) === 'ctrl-enter') mode = 'ctrl-enter'; } catch (_) {}
  const select = document.getElementById('send-key');
  if (select) {
    select.value = mode;
    select.addEventListener('change', () => {
      mode = select.value === 'ctrl-enter' ? 'ctrl-enter' : 'enter';
      try { localStorage.setItem(key, mode); } catch (_) {}
    });
  }
  for (const id of ['objective', 'instruction-text']) {
    const input = document.getElementById(id);
    if (!input) continue;
    let composing = false;
    input.addEventListener('compositionstart', () => { composing = true; });
    input.addEventListener('compositionend', () => { composing = false; });
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing || composing || event.keyCode === 229 ||
          event.shiftKey || event.altKey || input.disabled || input.readOnly) return;
      if (mode === 'ctrl-enter' && !event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const form = input.form;
      if (event.repeat || !form || form.querySelector('[type="submit"]:disabled')) return;
      form.requestSubmit();
    });
  }
  return {get mode() { return mode; }};
})();
