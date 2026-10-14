/**
 * The primitives on one page, for a visual check in a development build: open the app at
 * `#/ui` (main.tsx renders this instead of the app, and only when import.meta.env.DEV).
 * The theme button flips light and dark.
 */
import { IconDots, IconFile, IconMessage } from '@tabler/icons-react';
import { useState } from 'react';
import { Badge } from './Badge';
import { Button, IconButton } from './Button';
import { Card, CardBody, CardFooter, CardHeader, CardLead, CardSection } from './Card';
import { Dialog } from './Dialog';
import { Facts } from './Facts';
import { Field, Input, TextArea } from './Field';
import { Meter } from './Meter';
import { PageHeader } from './PageHeader';
import { Row, RowGroup, RowList } from './RowList';
import { Segmented } from './Segmented';
import { Sheet } from './Sheet';
import { StatusMark } from './StatusMark';
import { Working } from './Working';

const FACTS = [
  { value: "Read a receipt's text and give back the total, subtotal, tax and item count", label: 'Job', wide: true },
  { value: 'Receipt text', label: 'Takes' },
  { value: 'Total, subtotal, tax, item count', label: 'Gives back' },
  { value: '100 practice, 100 exam', label: 'Cases' },
  { value: '40 of 100', label: 'Pass mark' },
  { value: 'Lightning, Super, Ultra', label: 'Models tried' },
  { value: '$0.60, 60 min', label: 'Build limit' },
];

export function Showroom() {
  const [dark, setDark] = useState(document.documentElement.dataset.theme === 'dark');
  const [where, setWhere] = useState('here');
  const [tab, setTab] = useState('plan');
  const [dialog, setDialog] = useState(false);
  const [sheet, setSheet] = useState(false);
  const flip = () => {
    const next = !dark;
    document.documentElement.dataset.theme = next ? 'dark' : 'light';
    setDark(next);
  };
  return (
    <div style={{ height: '100vh', overflow: 'auto', background: 'hsl(var(--bg-subtle))' }}>
      <div style={{ maxWidth: 1000, margin: '0 auto', padding: '32px 24px 96px', display: 'flex', flexDirection: 'column', gap: 24 }}>
        <PageHeader title="Primitives" count={12} actions={<><Button size="sm" onClick={flip}>{dark ? 'Light' : 'Dark'}</Button><IconButton icon={IconMessage} label="Transcript" /></>} />

        <Card width="form" data-phase="plan">
          <CardHeader meta="receipt-reader" action={<IconButton icon={IconDots} label="More" />}>Plan</CardHeader>
          <CardBody><Facts items={FACTS} /></CardBody>
          <CardFooter><Button variant="primary" size="sm" data-action="build">Build for up to $0.60</Button></CardFooter>
        </Card>

        <Card width="list" data-phase="licensed">
          <CardHeader
            meta={<><StatusMark tone="ok" label="Passed" showLabel /><span>receipt-reader</span><span>18:54</span></>}
            lead={<CardLead value="64 / 100" note="Super ships" />}
            action={<IconButton icon={IconDots} label="More" />}
          >Exam result</CardHeader>
          <CardBody>
            <form className="try-form" style={{ display: 'flex', gap: 12, alignItems: 'flex-end' }} onSubmit={(e) => e.preventDefault()}>
              <div style={{ flex: 1 }}><Field label="Receipt" htmlFor="sr-receipt"><TextArea id="sr-receipt" /></Field></div>
              <Button size="sm" type="submit" data-action="run">Run</Button>
            </form>
          </CardBody>
          <CardFooter start="$0.32"><Button variant="primary" size="sm" data-action="add">Add to project</Button></CardFooter>
        </Card>

        <Card width="form" tone="attention" attentionNote="The build stopped">
          <CardHeader meta={<><StatusMark tone="neutral" label="Stopped" showLabel /><span>4:12</span></>}>Building</CardHeader>
          <CardBody><p className="text-small" style={{ color: 'hsl(var(--err))' }}>The relay refused the call (too many requests)</p></CardBody>
          <CardFooter><Button variant="primary" size="sm" data-action="again">Build again</Button></CardFooter>
        </Card>

        <Card width="form">
          <CardHeader meta={<><StatusMark tone="ok" label="Added" showLabel /><span>receipt-reader v1</span></>}>In your project</CardHeader>
          <CardBody pad="flush">
            <RowList>
              <RowGroup label="agents/receipt_reader/" count={3} />
              <Row leading={<IconFile size={16} stroke={1.5} />} title="agent.py" />
              <Row leading={<IconFile size={16} stroke={1.5} />} title="receipt-reader.spec.json" meta="Spec" />
              <Row leading={<IconFile size={16} stroke={1.5} />} title="report.signed.json" trailing={<StatusMark tone="ok" label="Signed" />} onSelect={() => undefined} />
            </RowList>
          </CardBody>
          <CardFooter divided><Button variant="primary" size="sm" data-action="copy">Copy line</Button></CardFooter>
        </Card>

        <Card width="form">
          <CardHeader>Controls</CardHeader>
          <CardBody style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
              <Button variant="primary">Primary</Button>
              <Button>Outline</Button>
              <Button variant="ghost">Ghost</Button>
              <Button variant="destructive">Delete</Button>
              <Button variant="link">Link</Button>
              <Button variant="primary" pending>Run</Button>
              <Button size="xs">xs</Button>
              <Button size="lg">lg</Button>
            </div>
            <Segmented kind="radio" label="Where will this agent live?" value={where} onChange={setWhere}
              options={[{ value: 'here', label: 'Run it here' }, { value: 'software', label: 'Put it into software' }]} />
            <Segmented kind="tabs" label="View" size="md" value={tab} onChange={setTab}
              options={[{ value: 'plan', label: 'Plan' }, { value: 'report', label: 'Report' }, { value: 'files', label: 'Files' }]} />
            <Field label="Judge code" htmlFor="sr-code"><Input id="sr-code" placeholder="No limit" /></Field>
            <Field label="Spare code" htmlFor="sr-bad" error="That code was refused"><Input id="sr-bad" invalid defaultValue="abc" /></Field>
            <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
              {(['ok', 'warn', 'err', 'neutral', 'accent'] as const).map((t) => <StatusMark key={t} tone={t} label={t} />)}
              <Badge>Recommended</Badge><Badge variant="count">3</Badge>
              <Meter value={12} max={20} size="row" label="Context" />
              <Working size="sm" />
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <Button onClick={() => setDialog(true)}>Open the dialog</Button>
              <Button onClick={() => setSheet(true)}>Open the sheet</Button>
            </div>
          </CardBody>
        </Card>
      </div>
      <Dialog open={dialog} onOpenChange={setDialog} title="Add receipt-reader to this project?"
        footerStart={<Button variant="link" size="sm">Say why</Button>}
        footer={<><Button size="sm" onClick={() => setDialog(false)}>Don't add</Button><Button variant="primary" size="sm" data-action="add">Add to project</Button></>} />
      <Sheet open={sheet} onOpenChange={setSheet} title="Plan" name="plan" footer={<Button variant="primary" size="sm">Build for up to $0.60</Button>}>
        <CardSection title="Spec"><Facts items={FACTS} columns={1} /></CardSection>
        <CardSection title="Next build"><Field label="Build limit" htmlFor="sr-limit"><Input id="sr-limit" size="sm" defaultValue="0.60" /></Field></CardSection>
      </Sheet>
    </div>
  );
}
