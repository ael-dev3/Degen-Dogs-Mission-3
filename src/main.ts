import './styles.css';
import { facts, modules, queries, sources, terminalLines } from './data';
import type { Fact, Module, QueryFile, Source } from './data';

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing element: ${id}`);
  }
  return element as T;
};

const createElement = <K extends keyof HTMLElementTagNameMap>(
  tagName: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] => {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

const stateLabel = (state: QueryFile['state'] | Module['state']): string => {
  if (state === 'ready') return 'ready';
  if (state === 'parameterized') return 'parameterized';
  return 'needs address';
};

const renderFacts = (items: Fact[]) => {
  const grid = byId<HTMLDivElement>('fact-grid');
  const fragment = document.createDocumentFragment();

  items.forEach((item) => {
    const card = createElement('article', 'fact-card reveal-child');
    const label = createElement('span', 'fact-label', item.label);
    const value = createElement('strong', 'fact-value', item.value);
    const detail = createElement('p', 'fact-detail', item.detail);
    card.append(label, value, detail);
    fragment.append(card);
  });

  grid.append(fragment);
};

const renderModules = (items: Module[]) => {
  const grid = byId<HTMLDivElement>('module-grid');
  const fragment = document.createDocumentFragment();

  items.forEach((item) => {
    const card = createElement('article', 'module-card reveal-child');
    card.dataset.state = item.state;

    const top = createElement('div', 'module-top');
    top.append(createElement('span', 'module-metric', item.metric), createElement('span', 'module-state', stateLabel(item.state)));

    const title = createElement('h3', undefined, item.title);
    const description = createElement('p', undefined, item.description);

    card.append(top, title, description);
    fragment.append(card);
  });

  grid.append(fragment);
};

const renderQueries = (items: QueryFile[]) => {
  const grid = byId<HTMLDivElement>('query-grid');
  const fragment = document.createDocumentFragment();

  items.forEach((item) => {
    const card = createElement('article', 'query-card reveal-child');
    card.dataset.state = item.state;

    const head = createElement('div', 'query-head');
    const file = createElement('code', undefined, item.file);
    const state = createElement('span', 'query-state', stateLabel(item.state));
    head.append(file, state);

    const title = createElement('h3', undefined, item.title);
    const purpose = createElement('p', undefined, item.purpose);
    const link = createElement('a', 'query-link', 'Open file');
    link.href = `https://github.com/ael-dev3/Degen-Dogs-Mission-3/blob/main/${item.file}`;
    link.rel = 'noreferrer';

    card.append(head, title, purpose, link);
    fragment.append(card);
  });

  grid.append(fragment);
};

const renderSources = (items: Source[]) => {
  const list = byId<HTMLDivElement>('source-list');
  const fragment = document.createDocumentFragment();

  items.forEach((item) => {
    const link = createElement('a', 'source-row');
    link.href = item.href;
    link.rel = 'noreferrer';

    const copy = createElement('span');
    copy.append(createElement('strong', undefined, item.title), createElement('small', undefined, item.detail));
    const arrow = createElement('span', 'source-arrow', '↗');
    link.append(copy, arrow);
    fragment.append(link);
  });

  list.append(fragment);
};

const renderTerminal = (lines: string[]) => {
  const container = byId<HTMLDivElement>('terminal-lines');
  const fragment = document.createDocumentFragment();

  lines.forEach((line, index) => {
    const row = createElement('div', 'terminal-line');
    row.style.setProperty('--delay', `${index * 90}ms`);
    row.append(createElement('span', undefined, '$'), createElement('code', undefined, line));
    fragment.append(row);
  });

  container.append(fragment);
};

const activateReveal = () => {
  const elements = Array.from(document.querySelectorAll<HTMLElement>('[data-reveal], .reveal-child'));

  if (!('IntersectionObserver' in window)) {
    elements.forEach((element) => element.classList.add('is-visible'));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: '0px 0px -12% 0px', threshold: 0.08 },
  );

  elements.forEach((element) => observer.observe(element));
};

const wireHeader = () => {
  const header = document.querySelector<HTMLElement>('.site-header');
  if (!header) return;

  const onScroll = () => {
    header.dataset.scrolled = window.scrollY > 16 ? 'true' : 'false';
  };

  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });
};

renderFacts(facts);
renderModules(modules);
renderQueries(queries);
renderSources(sources);
renderTerminal(terminalLines);
activateReveal();
wireHeader();
