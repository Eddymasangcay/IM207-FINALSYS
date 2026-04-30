(function () {
    'use strict';

    function initNavToggle() {
        var toggle = document.getElementById('site-nav-toggle');
        var nav = document.getElementById('site-nav-panel');
        var logoTrigger = document.getElementById('site-logo-trigger');
        if (!toggle || !nav) return;

        function setNavState(open) {
            nav.classList.toggle('is-open', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            toggle.setAttribute('aria-label', open ? 'Close main menu' : 'Open main menu');
            document.body.classList.toggle('ck-nav-open', open);
        }

        function closeNav() {
            setNavState(false);
        }

        toggle.addEventListener('click', function () {
            var open = !nav.classList.contains('is-open');
            setNavState(open);
        });

        if (logoTrigger) {
            logoTrigger.addEventListener('click', function (event) {
                if (!window.matchMedia('(max-width: 900px)').matches) return;
                event.preventDefault();
                var open = !nav.classList.contains('is-open');
                setNavState(open);
            });
        }

        nav.querySelectorAll('a').forEach(function (link) {
            link.addEventListener('click', function () {
                if (window.matchMedia('(max-width: 900px)').matches) {
                    closeNav();
                }
            });
        });

        document.addEventListener('click', function (event) {
            if (!window.matchMedia('(max-width: 900px)').matches) return;
            if (!nav.classList.contains('is-open')) return;
            if (toggle.contains(event.target) || nav.contains(event.target)) return;
            closeNav();
        });

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && nav.classList.contains('is-open')) {
                closeNav();
            }
        });

        window.addEventListener('resize', function () {
            if (!window.matchMedia('(max-width: 900px)').matches) {
                closeNav();
            }
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
