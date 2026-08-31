---
title: Pinard
---

<!-- NAV -->
<nav class="nav" id="main-nav">
  <a class="nav-logo" href="/">Pin<span>ard</span></a>
  <ul class="nav-links">
    <li><a href="#the-estate">The Estate</a></li>
    <li><a href="#features">The Cellar</a></li>
    <li><a href="/docs/">The Craft</a></li>
  </ul>
  <a class="nav-cta" href="https://github.com/Genentech/pinard">GitHub →</a>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-bg" id="hero-bg"></div>
  <div class="hero-overlay"></div>
  <div class="hero-content">
    <h1 class="hero-title">Orchestrate your agents<br>like a <em>Grand Cru</em></h1>
    <p class="hero-subtitle">Pinard runs fleets of AI agents as <em>semi-deterministic loops</em> — deterministic runbooks that drive non-deterministic agents — coordinated across many repositories and machines, and remembering what they learn.</p>
    <div class="hero-actions">
      <a class="btn-primary" href="#the-estate">Discover the Estate</a>
      <a class="btn-ghost" href="https://github.com/Genentech/pinard">View on GitHub</a>
    </div>
  </div>
  <div class="hero-scroll">
    <div class="hero-scroll-line"></div>
    <span>Scroll</span>
  </div>
</section>

<!-- INTRO -->
<section class="section-intro" id="the-estate">
  <div class="section-intro-inner reveal">
    <p class="label">The Estate</p>
    <h2>Great software, like great wine,<br>is the art of the blend</h2>
    <p>
      Pinard is named after the French slang for <em>everyday wine</em> — unpretentious, honest, <strong>gets the job done</strong>.
      It brings winemaking metaphors to multi-repo development: your workspace is a <strong>vignoble</strong> (wine estate),
      each repository is a <strong>vigne</strong> (vine), and coordinated changes flow through <strong>cuvée</strong> branches
      before reaching main. A <strong>régisseur</strong> conducts the whole estate, a <strong>maître</strong> leads each
      parcelle (workstream), and <strong>vendangeurs</strong> bring in the harvest. One conductor. Many agents. One harvest.
    </p>
    <p>
      Underneath the metaphor, Pinard is an <strong>engine</strong>: it wraps every agent in a
      <strong>semi-deterministic loop</strong> — where the control flow is code and only the work inside
      a step is left to the model — so agent work is reliable, resumable, and auditable. Three pillars hold it up.
    </p>
    <p class="intro-pillars"><strong>Deterministic control</strong> · <strong>Non-deterministic agents</strong> · <strong>Persistent memory</strong></p>
  </div>
</section>

<!-- CHAPTERS -->
<div class="chapters">

  <article class="chapter" id="vignoble">
    <div class="chapter-photo">
      <img src="/images/photos/chapter-vignoble.jpg" alt="A wine estate divided into distinct vineyard parcels around one central manor" loading="lazy">
      <div class="chapter-photo-overlay"></div>
    </div>
    <div class="chapter-body reveal">
      <span class="chapter-number">01</span>
      <p class="chapter-label">The Vignoble</p>
      <h2 class="chapter-title">Your workspace<br>is an <em>estate</em></h2>
      <p class="chapter-text">
        A vignoble is a wine estate — a landscape of vines tended by one winemaker.
        In Pinard, your <strong>vignoble</strong> is the collection of all repositories you manage,
        defined by a single <code style="font-family:var(--mono);font-size:0.85em;color:var(--oak-lt)">vignes.yaml</code> at the root.
        Each repository is a <strong>vigne</strong> — its own soil, its own character,
        tended by agents who understand its terroir.
      </p>
      <blockquote class="chapter-quote">
        Every great vineyard begins with knowing your land.
      </blockquote>
    </div>
  </article>

  <article class="chapter">
    <div class="chapter-photo">
      <img src="/images/photos/chapter-cuvee.jpg" alt="Several source barrels feeding one central blending cask" loading="lazy">
      <div class="chapter-photo-overlay"></div>
    </div>
    <div class="chapter-body reveal">
      <span class="chapter-number">02</span>
      <p class="chapter-label">The Cuvée</p>
      <h2 class="chapter-title">Blending agents<br>into a <em>cuvée</em></h2>
      <p class="chapter-text">
        When multiple AI agents work on the same repository, their concurrent merge requests
        can create conflicts — like fermenting barrels that can't be combined too early.
        Pinard's <strong>cuvée strategy</strong> routes all agents through an intermediate branch,
        letting each barrel age in sequence before the final assemblage to <code style="font-family:var(--mono);font-size:0.85em;color:var(--oak-lt)">main</code>.
      </p>
      <blockquote class="chapter-quote">
        The cuvée is not a compromise. It is the art of the blend.
      </blockquote>
    </div>
  </article>

  <article class="chapter">
    <div class="chapter-photo">
      <img src="/images/photos/chapter-recolte.jpg" alt="A final bottle being placed into a completed harvest crate" loading="lazy">
      <div class="chapter-photo-overlay"></div>
    </div>
    <div class="chapter-body reveal">
      <span class="chapter-number">03</span>
      <p class="chapter-label">La Récolte</p>
      <h2 class="chapter-title">Every agent<br>works toward <em>harvest</em></h2>
      <p class="chapter-text">
        The <strong>récolte</strong> — the harvest — is the moment a loop's work lands: code merged,
        a pipeline run, data shipped, and the vintage bottled. For code loops, Pinard's MR watcher
        monitors every open merge request, forwards review comments to the right agent, triggers
        auto-merge when quality gates pass, and cleans up sessions when the harvest is done.
      </p>
      <blockquote class="chapter-quote">
        Patience and precision. The harvest does not rush.
      </blockquote>
    </div>
  </article>

  <article class="chapter">
    <div class="chapter-photo">
      <img src="/images/photos/chapter-terroir.jpg" alt="Vine cuttings growing in distinct soil samples beside a field notebook" loading="lazy">
      <div class="chapter-photo-overlay"></div>
    </div>
    <div class="chapter-body reveal">
      <span class="chapter-number">04</span>
      <p class="chapter-label">Le Terroir</p>
      <h2 class="chapter-title">Each repo has<br>its own <em>terroir</em></h2>
      <p class="chapter-text">
        In winemaking, terroir is the sum of soil, climate, and tradition — the invisible hand
        that shapes every grape. In Pinard, <strong>terroir</strong> lives in
        <code style="font-family:var(--mono);font-size:0.85em;color:var(--oak-lt)">VIGNE.md</code>:
        per-repo instructions loaded into every agent's context. Run these tests.
        Follow these conventions. Never touch these files. The agent inherits the character of the land.
      </p>
      <blockquote class="chapter-quote">
        You cannot fake terroir. It must be lived and passed on.
      </blockquote>
    </div>
  </article>

</div>

<!-- FEATURES -->
<section class="section-features" id="features">
  <div class="section-features-inner">
    <div class="section-header reveal">
      <p class="label">The Cellar</p>
      <h2>What's in the bottle</h2>
    </div>
    <div class="features-grid reveal">
      <h3 class="features-subhead">The Engine</h3>
      <a class="feature-card" href="/features/processes/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-processes.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Semi-Deterministic Loops</div>
          <p class="feature-text">Runbooks as code: ordered, checkpointed, resumable agent steps — the core of Pinard. Deterministic control, model steps only where judgment is needed.</p>
          <div class="feature-footer">
            <span class="feature-tag">process.js</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/agents/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-agents.jpg'); --photo-position: center top"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Independent Agents</div>
          <p class="feature-text">One Pi agent session per task. Each agent works in isolation, aware of its vigne's terroir via VIGNE.md.</p>
          <div class="feature-footer">
            <span class="feature-tag">spawn_agent()</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/memory/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-memory.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Persistent Memory</div>
          <p class="feature-text">Local-first, curated, portable memory. Agents remember decisions, fixes, and lessons across sessions — the estate learns instead of relearning.</p>
          <div class="feature-footer">
            <span class="feature-tag">mem_*</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card feature-card-wide" href="/features/domaines/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-domaines.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Distributed — Les Domaines</div>
          <p class="feature-text">One conductor orchestrating agents across machines, clusters, and cloud — remote and HPC workers already run over NATS.</p>
          <div class="feature-footer">
            <span class="feature-tag">over NATS</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <h3 class="features-subhead">Orchestration &amp; Control</h3>
      <a class="feature-card" href="/features/parcelles/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-parcelles.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Parcelles &amp; the Crew</div>
          <p class="feature-text">Group work into workstreams, each with its own conductor — a régisseur / maître / vendangeur crew that scales to many parallel efforts.</p>
          <div class="feature-footer">
            <span class="feature-tag">parcelle.yaml</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/web-terminal/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-web-terminal.jpg'); --photo-position: center 78%"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Live Terminals &amp; Control Room</div>
          <p class="feature-text">Watch — or steer — any agent's live session from the browser, streamed over NATS. No SSH, even for remote and HPC workers.</p>
          <div class="feature-footer">
            <span class="feature-tag">/sessions</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/scheduling/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-scheduling.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Scheduled Harvests</div>
          <p class="feature-text">Cron-driven agent spawns for nightly linting, weekly dependency updates, daily security audits.</p>
          <div class="feature-footer">
            <span class="feature-tag">schedules.yaml</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <h3 class="features-subhead">The SWE Vintage <span class="features-subhead-note">— built-in loop for software development</span></h3>
      <a class="feature-card" href="/features/issue-workflow/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-issue-workflow.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Issue-Driven Work</div>
          <p class="feature-text">Assign a GitLab issue and a vendangeur spawns itself, does the work, opens an MR, and handles review — hands-free.</p>
          <div class="feature-footer">
            <span class="feature-tag">assign → spawn</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/openspec-dispatch/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-openspec-dispatch.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">OpenSpec Dispatch</div>
          <p class="feature-text">Author an OpenSpec change (proposal + task checklist), then turn it into GitLab issues and spawned agents with one command.</p>
          <div class="feature-footer">
            <span class="feature-tag">/dispatch</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/cuvee/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-cuvee-branching.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Cuvée Branching</div>
          <p class="feature-text">Batch concurrent MRs through an intermediate branch when agents target the same repository.</p>
          <div class="feature-footer">
            <span class="feature-tag">cuvee/&lt;name&gt;</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/auto-merge/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-auto-merge.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Auto-Merge &amp; Watch</div>
          <p class="feature-text">MRs merge when approved and CI passes. Post-merge pipelines monitored for failures.</p>
          <div class="feature-footer">
            <span class="feature-tag">auto_merge: true</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/review-forwarding/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-review-forwarding.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Review Forwarding</div>
          <p class="feature-text">MR review comments routed back to the right agent. Agents stay alive to address feedback.</p>
          <div class="feature-footer">
            <span class="feature-tag">MR watcher</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
      <a class="feature-card" href="/features/pipeline-recovery/">
        <div class="feature-card-bg" style="background-image: url('/images/photos/feature-pipeline-recovery.jpg'); --photo-position: center"></div>
        <div class="feature-card-overlay"></div>
        <div class="feature-card-body">
          <div class="feature-title">Pipeline Retry &amp; Recovery</div>
          <p class="feature-text">Failed MR pipelines return to the same vendangeur for a bounded repair loop. Repeated failure trips the circuit breaker.</p>
          <div class="feature-footer">
            <span class="feature-tag">attempt X/5</span>
            <span class="feature-arrow">→</span>
          </div>
        </div>
      </a>
    </div>
  </div>
</section>

<!-- PHILOSOPHY CONCLUSION -->
<section class="section-craft" id="philosophy">
  <div class="section-craft-bg"></div>
  <div class="section-craft-overlay"></div>
  <div class="section-craft-content reveal">
    <p class="craft-quote">The best code, like the best wine, is not the result of a single hand — but of many, working in concert, guided by a conductor who knows when to intervene and when to let the process breathe.</p>
    <p class="craft-attr">The Pinard Philosophy</p>
  </div>
</section>

<!-- CTA -->
<section class="section-cta">
  <div class="section-cta-inner reveal">
    <h2>Ready to uncork your vignoble?</h2>
    <p>Pinard runs wherever you have Pi and GitLab. Start with a single vigne, grow to an entire estate.</p>
    <div class="cta-actions">
      <a class="btn-cream" href="https://github.com/Genentech/pinard">View on GitHub</a>
      <a class="btn-outline-cream" href="https://github.com/Genentech/pinard/blob/main/README.md">Read the README</a>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer class="footer">
  <div class="footer-logo">Pin<span>ard</span></div>
  <ul class="footer-links">
    <li><a href="https://github.com/Genentech/pinard">GitHub</a></li>
    <li><a href="/docs/">The Craft</a></li>
    <li><a href="https://github.com/Genentech/pinard/blob/main/PINARD.md">PINARD.md</a></li>
  </ul>
  <p class="footer-credit">
    Original artwork created for Pinard ·
    Built with <a href="https://gohugo.io" target="_blank">Hugo</a>
  </p>
</footer>

<!-- JS: nav scroll + reveal -->
<script>
(function() {
  // Nav scroll effect
  var nav = document.getElementById('main-nav');
  window.addEventListener('scroll', function() {
    nav.classList.toggle('scrolled', window.scrollY > 60);
  }, { passive: true });

  // Hero bg parallax trigger
  var heroBg = document.getElementById('hero-bg');
  if (heroBg) {
    window.addEventListener('load', function() {
      heroBg.classList.add('loaded');
    });
  }

  // Scroll reveal
  var reveals = document.querySelectorAll('.reveal');
  var observer = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
  reveals.forEach(function(el) { observer.observe(el); });

})();
</script>
