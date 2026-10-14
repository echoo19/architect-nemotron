/**
 * The first time Architect works in a folder, it asks (Claude Code's workspace
 * trust). A folder can carry its own instructions and tool servers, which run
 * code, so nothing from it loads until you say so.
 */
import { useEffect } from 'react';
import { useApp } from '../../store/app';
import { chooseFolder } from '../shell/folders';
import { Button } from '../ui/Button';
import { Card, CardBody, CardFooter, CardHeader } from '../ui/Card';

export function TrustFolder({ id, cwd }: { id: string; cwd: string }) {
  const trustFolder = useApp((s) => s.trustFolder);
  const closeConversation = useApp((s) => s.closeConversation);
  const openConversation = useApp((s) => s.openConversation);
  const name = cwd.split(/[\\/]/).filter(Boolean).pop() ?? cwd;
  // Enter trusts the folder while nothing else has focus: the button is not focused on
  // load, so it does not wear a focus ring before anyone has pressed a key.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Enter' && (document.activeElement === document.body || !document.activeElement)) {
        e.preventDefault();
        trustFolder(id);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [id, trustFolder]);
  return (
    <div className="conversation-row">
      <section className="conversation trust">
        <Card cellClassName="trust-cell" className="trust-panel" role="dialog" aria-label={`Work in ${name}?`}>
          <CardHeader meta={<span className="trust-where" title={cwd}>{cwd}</span>}>Work in {name}?</CardHeader>
          <CardBody>
            <ul className="trust-list">
              <li>Architect reads the files in this folder and follows its ARCHITECT.md.</li>
              <li>Tool servers the folder sets up can run on this computer.</li>
              <li>Edits and commands still follow your permission mode.</li>
              <li>Trust only folders whose contents you know.</li>
            </ul>
          </CardBody>
          <CardFooter
            start={(
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void chooseFolder(cwd).then((next) => {
                  if (!next) return;
                  closeConversation(id);
                  openConversation({ cwd: next });
                })}
              >
                Choose another folder
              </Button>
            )}
          >
            <Button variant="primary" size="sm" onClick={() => trustFolder(id)}>Trust this folder</Button>
          </CardFooter>
        </Card>
      </section>
    </div>
  );
}
