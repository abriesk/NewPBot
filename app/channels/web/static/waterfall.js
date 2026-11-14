/* NP6: the waterfall booking flow's only script. The page works without it (every answer is a link or a form
   post); this adds what htmx cannot: a short wait before the swap so the tapped answer can ripple and the old
   question settle, then scrolling the new question into view, putting the keyboard in it and saying "Step n of m"
   and the question aloud. A failed request falls back to a normal page load, so a tap never dies silently.
   Nothing here times, counts or rewards anything. With reduced motion there is no wait and no smooth scrolling. */
(function () {
  var query = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var calm = function () { return query.matches; };

  if (window.htmx) {
    window.htmx.config.defaultSwapDelay = calm() ? 0 : 240;
    window.htmx.config.defaultSettleDelay = 0;
    window.htmx.config.scrollBehavior = calm() ? 'auto' : 'smooth';
  }

  // The ripple starts where the finger or pointer touched the answer.
  document.addEventListener('click', function (event) {
    var answer = event.target.closest ? event.target.closest('.ans') : null;
    if (!answer || calm()) { return; }
    var box = answer.getBoundingClientRect();
    answer.style.setProperty('--rx', (event.clientX - box.left) + 'px');
    answer.style.setProperty('--ry', (event.clientY - box.top) + 'px');
    answer.classList.add('picked');
  }, true);

  // "Step 3 of 7. Choose a day": read out by the live region next to the path, which outlives every swap.
  function announce(current) {
    var live = document.getElementById('wf-live');
    if (!live || !current) { return; }
    var step = current.querySelector('.q-step');
    var heading = current.querySelector('h2');
    var text = ((step ? step.textContent.trim() + '. ' : '') + (heading ? heading.textContent.trim() : '')).trim();
    live.textContent = '';
    window.requestAnimationFrame(function () { live.textContent = text; });
  }

  function arrive(scroll) {
    var current = document.getElementById('q-current');
    if (!current) { return; }
    if (scroll) { current.scrollIntoView({ behavior: calm() ? 'auto' : 'smooth', block: 'start' }); }
    current.focus({ preventScroll: true });
    announce(current);
  }

  document.addEventListener('htmx:afterSettle', function (event) {
    if (event.target && event.target.id === 'path') { arrive(true); }
  });
  // Back and forward restore the path from htmx's history cache without a settle: same arrival.
  document.addEventListener('htmx:historyRestore', function () { arrive(false); });

  // A request that failed (server error, connection lost): do what the link or the form does without a script.
  function fallBack(event) {
    var el = event.detail && event.detail.elt;
    if (!el) { return; }
    if (el.tagName === 'A' && el.href) { window.location.href = el.href; return; }
    var form = el.tagName === 'FORM' ? el : (el.form || (el.closest ? el.closest('form') : null));
    if (!form) { return; }
    // form.submit() does not send the button that was pressed: put its name and value in.
    var config = event.detail.requestConfig;
    var pressed = config && config.triggeringEvent && config.triggeringEvent.submitter;
    if (!pressed && el.tagName === 'BUTTON') { pressed = el; }
    if (pressed && pressed.name) {
      var hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = pressed.name;
      hidden.value = pressed.value;
      form.appendChild(hidden);
      if (pressed.getAttribute('formaction')) { form.action = pressed.getAttribute('formaction'); }
    }
    form.removeAttribute('hx-post');
    form.submit();
  }
  document.addEventListener('htmx:responseError', fallBack);
  document.addEventListener('htmx:sendError', fallBack);
})();
