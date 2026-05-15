import './styles.css';
import { contractStatuses, dashboardPanels, kpis } from './data';
import type { ContractStatus, DashboardPanel, Kpi } from './data';

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing element: ${id}`);
  return element as T;
};

const el = <K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const statusText = (status: DashboardPanel['status'] | ContractStatus['status']): string => {
  switch (status) {
    case 'ready':
      return 'ready';
    case 'parameterized':
      return 'param';
    case 'discovery':
      return 'discover';
    case 'verified':
      return 'verified';
    case 'pending':
      return 'pending';
  }
};

const renderKpis = (items: Kpi[]) => {
  const grid = byId<HTMLDivElement>('kpi-grid');
  grid.replaceChildren(
    ...items.map((item) => {
      const card = el('article', 'kpi-card');
      if (item.tone) card.dataset.tone = item.tone;
      card.append(el('span', 'kpi-label', item.label), el('strong', 'kpi-value', item.value), el('small', 'kpi-sub', item.sub));
      return card;
    }),
  );
};

const renderPanelTable = (items: DashboardPanel[]) => {
  const table = byId<HTMLDivElement>('panel-table');
  table.replaceChildren(
    ...items.map((item) => {
      const row = el('a', 'table-row');
      row.href = `https://github.com/ael-dev3/Degen-Dogs-Mission-3/blob/main/${item.source}`;
      row.rel = 'noreferrer';
      row.dataset.status = item.status;
      row.append(el('span', undefined, item.name), el('code', undefined, item.source), el('strong', undefined, statusText(item.status)));
      return row;
    }),
  );
};

const renderContracts = (items: ContractStatus[]) => {
  const list = byId<HTMLDivElement>('contract-list');
  list.replaceChildren(
    ...items.map((item) => {
      const row = el('div', 'contract-row');
      row.dataset.status = item.status;
      row.append(el('span', undefined, item.label), el('code', undefined, item.address), el('strong', undefined, statusText(item.status)));
      return row;
    }),
  );
};

renderKpis(kpis);
renderPanelTable(dashboardPanels);
renderContracts(contractStatuses);
