import { useEffect, useState } from 'react';
import './scroll-motion.css';

const REVEAL = '.loop-story-step, .demo-section-head, .sponsor-grid > article, .demo-tools > h2';

/** Native scrolling stays in control; motion is an optional presentation layer. */
export default function ScrollMotion() {
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(() => typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches);

  useEffect(() => {
    const preference = matchMedia('(prefers-reduced-motion: reduce)');
    const change = () => setReduced(preference.matches);
    preference.addEventListener('change', change);
    return () => preference.removeEventListener('change', change);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    const stopped = paused || reduced;
    root.dataset.motion = stopped ? 'paused' : 'running';
    root.dataset.scrollMotion = stopped ? 'off' : 'on';
    if (stopped) return () => { delete root.dataset.motion; delete root.dataset.scrollMotion; };

    let scrollFrame = 0;
    let pointerFrame = 0;
    let target: HTMLElement | null = null;
    let pointerX = 0;
    let pointerY = 0;
    const fine = matchMedia('(hover: hover) and (pointer: fine)');
    const revealed = new Set<HTMLElement>();
    const touched = new Set<HTMLElement>();
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          (entry.target as HTMLElement).dataset.scrollReveal = 'visible';
          observer?.unobserve(entry.target);
        }
      }
    }, { threshold: 0.05, rootMargin: '0px 0px -24px 0px' });

    const register = (scope: ParentNode) => {
      const nodes = [...scope.querySelectorAll<HTMLElement>(REVEAL)];
      if (scope instanceof HTMLElement && scope.matches(REVEAL)) nodes.unshift(scope);
      for (const node of nodes) {
        if (revealed.has(node) || !observer) continue;
        revealed.add(node);
        // Never hide content already in view, including initial hero and results.
        if (node.getBoundingClientRect().top < innerHeight) node.dataset.scrollReveal = 'visible';
        else { node.dataset.scrollReveal = 'pending'; observer.observe(node); }
      }
    };
    const updateScroll = () => {
      scrollFrame = 0;
      const range = Math.max(1, root.scrollHeight - innerHeight);
      const progress = Math.max(0, Math.min(100, Math.round(scrollY / range * 100)));
      // Static CSS attribute rules keep all visual values outside inline styles.
      root.dataset.scrollProgress = String(progress);
    };
    const queueScroll = () => { if (!scrollFrame) scrollFrame = requestAnimationFrame(updateScroll); };
    const clearTilt = () => {
      if (target) { delete target.dataset.scrollTilt; delete target.dataset.tiltX; delete target.dataset.tiltY; }
      target = null;
    };
    const updatePointer = () => {
      pointerFrame = 0;
      if (!target?.isConnected) return clearTilt();
      const box = target.getBoundingClientRect();
      target.dataset.scrollTilt = 'active';
      target.dataset.tiltX = String(Math.round(Math.max(-2, Math.min(2, (0.5 - (pointerY - box.top) / Math.max(1, box.height)) * 4))));
      target.dataset.tiltY = String(Math.round(Math.max(-2, Math.min(2, ((pointerX - box.left) / Math.max(1, box.width) - 0.5) * 4))));
    };
    const pointerMove = (event: PointerEvent) => {
      if (!fine.matches || event.pointerType !== 'mouse') return clearTilt();
      const next = event.target instanceof Element ? event.target.closest<HTMLElement>('.evidence-card, .source') : null;
      if (next !== target) { clearTilt(); target = next; }
      if (!target) return;
      touched.add(target);
      pointerX = event.clientX; pointerY = event.clientY;
      if (!pointerFrame) pointerFrame = requestAnimationFrame(updatePointer);
    };
    const mutation = new MutationObserver(records => {
      for (const record of records) for (const node of record.addedNodes) if (node instanceof HTMLElement) register(node);
      queueScroll();
    });
    register(document);
    mutation.observe(document.body, { childList: true, subtree: true });
    updateScroll();
    addEventListener('scroll', queueScroll, { passive: true });
    addEventListener('resize', queueScroll, { passive: true });
    document.addEventListener('pointermove', pointerMove, { passive: true });
    document.documentElement.addEventListener('pointerleave', clearTilt);
    addEventListener('blur', clearTilt);
    return () => {
      cancelAnimationFrame(scrollFrame); cancelAnimationFrame(pointerFrame);
      observer?.disconnect(); mutation.disconnect(); clearTilt();
      removeEventListener('scroll', queueScroll); removeEventListener('resize', queueScroll);
      document.removeEventListener('pointermove', pointerMove);
      document.documentElement.removeEventListener('pointerleave', clearTilt);
      removeEventListener('blur', clearTilt);
      for (const node of revealed) delete node.dataset.scrollReveal;
      for (const node of touched) { delete node.dataset.scrollTilt; delete node.dataset.tiltX; delete node.dataset.tiltY; }
      delete root.dataset.motion; delete root.dataset.scrollMotion; delete root.dataset.scrollProgress;
    };
  }, [paused, reduced]);

  return <>
    <div className="scroll-motion-progress" aria-hidden="true" />
    <button type="button" className="motion-toggle scroll-motion-toggle" disabled={reduced} aria-pressed={paused || reduced} onClick={() => setPaused(value => !value)} title={reduced ? 'Motion follows your device preference' : undefined}>
      {reduced ? 'Motion off' : paused ? 'Resume motion' : 'Pause motion'}
    </button>
  </>;
}
