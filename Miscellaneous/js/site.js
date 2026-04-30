(function () {
    'use strict';

    function initNavToggle() {
        var toggle = document.getElementById('site-nav-toggle');
        var nav = document.getElementById('site-nav-panel');
        if (!toggle || !nav) return;

        toggle.addEventListener('click', function () {
            var open = nav.classList.toggle('is-open');
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            document.body.classList.toggle('ck-nav-open', open);
        });

        nav.querySelectorAll('a').forEach(function (link) {
            link.addEventListener('click', function () {
                if (window.matchMedia('(max-width: 900px)').matches) {
                    nav.classList.remove('is-open');
                    toggle.setAttribute('aria-expanded', 'false');
                    document.body.classList.remove('ck-nav-open');
                }
            });
        });
    }

    function initCopyButtons() {
        document.querySelectorAll('[data-copy]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var text = btn.getAttribute('data-copy') || '';
                if (!text) return;
                function done(ok) {
                    var prev = btn.getAttribute('data-label') || btn.textContent;
                    if (!btn.getAttribute('data-label')) btn.setAttribute('data-label', prev);
                    btn.textContent = ok ? 'Copied' : 'Copy failed';
                    setTimeout(function () {
                        btn.textContent = btn.getAttribute('data-label') || 'Copy';
                    }, 1600);
                }
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(function () { done(true); }).catch(function () { done(false); });
                } else {
                    try {
                        var ta = document.createElement('textarea');
                        ta.value = text;
                        ta.style.position = 'fixed';
                        ta.style.left = '-9999px';
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        document.body.removeChild(ta);
                        done(true);
                    } catch (e) {
                        done(false);
                    }
                }
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        initNavToggle();
        initCopyButtons();
    });
})();
