import type { ReactNode } from 'react';

export interface OverviewMetric {label: string; value: ReactNode; note?: string;}
export function OverviewStats({items}: {items: OverviewMetric[]}) {
  return <section className="overview-stats" aria-label="页面概览">{items.map(item => <article className="overview-stat" key={item.label}><span>{item.label}</span><strong>{item.value ?? '—'}</strong>{item.note && <small>{item.note}</small>}</article>)}</section>;
}
