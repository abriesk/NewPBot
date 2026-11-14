/* NP6: the waterfall booking flow's only script. The page works without it (every answer is a link);
   this adds what htmx cannot: a short wait before the swap so the tapped answer can ripple and the old
   question settle, then scrolling the new question into view and putting the keyboard in it. Nothing
   here times, counts or rewards anything. With reduced motion there is no wait and no smooth scrolling. */
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

  document.addEventListener('htmx:afterSettle', function (event) {
    var current = document.getElementById('q-current');
    if (!current || !event.target || event.target.id !== 'path') { return; }
    current.scrollIntoView({ behavior: calm() ? 'auto' : 'smooth', block: 'start' });
    current.focus({ preventScroll: true });
  });
})();
