import { useEffect, useState, type CSSProperties } from "react";

/** A dimensional film loop; geometry is decorative, never experimental evidence. */
export function LoopSculpture({ compact = false }: { compact?: boolean }) {
  return <span className={`loop-sculpture${compact ? " is-compact" : ""}`} aria-hidden="true"><span className="loop-camera"><span className="loop-rotor">{Array.from({length:18},(_,i) => <i key={i} style={{"--slice":i} as CSSProperties}><b /><b /></i>)}</span></span><span className="loop-shadow" /></span>;
}

const REVEALS = ".hero>*,.input-card,.landing-loop,.landing-footnote,.loop-story-step,.study-header,.chapter-nav,.beat-reading,.hyp,.study-ranking,.selected-experiment,.comparison-cinema,.comparison-evidence,.study-learning,.memory-page-head,.transfer-chain>div,.ranking-reveal-head,.rank-transfer,.transfer-explanation,.transfer-outcome,.experiment-library>li,.strategy,.review-support,.study-technical";

/** Reveal real content on entry; keep scrolling native and cancel motion on request. */
export function MotionDirector() {
  const [paused,setPaused] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const change = () => setPaused(preference.matches);
    preference.addEventListener("change",change);
    return () => preference.removeEventListener("change",change);
  },[]);
  useEffect(() => {
    document.documentElement.dataset.motion = paused ? "paused" : "active";
    if(paused) return;
    const main = document.getElementById("main");
    if(!main) return;
    const seen = new WeakSet<Element>();
    const animations = new Set<Animation>();
    const observer = new IntersectionObserver(entries => {
      entries.filter(e => e.isIntersecting).forEach((entry,index) => {
        observer.unobserve(entry.target);
        const animation = entry.target.animate([
          {opacity:0,transform:"perspective(1200px) translateY(28px) rotateX(3deg)",filter:"blur(3px)"},
          {opacity:1,transform:"perspective(1200px) translateY(0) rotateX(0)",filter:"blur(0)"},
        ],{duration:760,delay:Math.min(index*65,260),easing:"cubic-bezier(.16,1,.3,1)",fill:"backwards"});
        animations.add(animation);
        animation.onfinish = () => animations.delete(animation);
      });
    },{threshold:.08});
    const scan = () => main.querySelectorAll(REVEALS).forEach(el => {
      if(!seen.has(el)) { seen.add(el); observer.observe(el); }
    });
    scan();
    const mutations = new MutationObserver(scan);
    mutations.observe(main,{childList:true,subtree:true});
    let frame = 0;
    const scroll = () => {
      if(frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const total = document.documentElement.scrollHeight-innerHeight;
        document.documentElement.style.setProperty("--page-progress",String(total > 0 ? scrollY/total : 0));
        main.querySelectorAll<HTMLElement>(".loop-story").forEach(el => {
          const progress = Math.max(0,Math.min(1,(innerHeight-el.getBoundingClientRect().top)/(innerHeight+el.offsetHeight)));
          el.style.setProperty("--story-turn",`${progress*150-50}deg`);
        });
      });
    };
    const pointer = (event: PointerEvent) => {
      if(event.pointerType !== "mouse") return;
      const surface = (event.target as Element).closest<HTMLElement>("[data-tilt]");
      if(!surface) return;
      const r = surface.getBoundingClientRect();
      surface.style.setProperty("--tilt-y",`${((event.clientX-r.left)/r.width-.5)*5}deg`);
      surface.style.setProperty("--tilt-x",`${((event.clientY-r.top)/r.height-.5)*-4}deg`);
    };
    const leave = (event: PointerEvent) => {
      const surface = (event.target as Element).closest<HTMLElement>("[data-tilt]");
      if(surface && !surface.contains(event.relatedTarget as Node | null)) {
        surface.style.setProperty("--tilt-y","0deg"); surface.style.setProperty("--tilt-x","0deg");
      }
    };
    const visibility = () => { document.documentElement.dataset.motion = document.hidden ? "paused" : "active"; };
    window.addEventListener("scroll",scroll,{passive:true});
    main.addEventListener("pointermove",pointer,{passive:true});
    main.addEventListener("pointerout",leave,{passive:true});
    document.addEventListener("visibilitychange",visibility);
    scroll();
    return () => {
      observer.disconnect(); mutations.disconnect(); animations.forEach(a => a.cancel()); cancelAnimationFrame(frame);
      window.removeEventListener("scroll",scroll); main.removeEventListener("pointermove",pointer); main.removeEventListener("pointerout",leave); document.removeEventListener("visibilitychange",visibility);
    };
  },[paused]);
  return <button className="motion-toggle" type="button" aria-pressed={paused} onClick={() => setPaused(p => !p)}><span aria-hidden="true">{paused ? "▷" : "Ⅱ"}</span>{paused ? "Motion paused" : "Pause motion"}</button>;
}
