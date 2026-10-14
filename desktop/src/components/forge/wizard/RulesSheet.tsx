/**
 * The rules sheet (clarity spec, 2b): the task's rules as the plan states them, one per
 * line, each split at its first ":" into the field it is about and what it says. Then,
 * when the plan has them, the rules code checks on every answer, exactly as the plan
 * words them: they name output fields, not task lines, so they get their own list (C9).
 *
 * Its secondary is "Edit plan", the same words as the Plan screen's button, and it opens
 * the same settings: they change the pass mark, budgets and models, never the task's
 * words, so nothing here promises to edit a rule. A plan built from a file has no such
 * button here: its rules change in the file, which the intro says. Each rule reads one sentence to a line,
 * and the intro says whose words they are (the plan file's) and which data they were
 * written for, so labels such as PPN or DPP read as the data's own.
 */
import type { ForgeModel } from '../../../lib/forge';
import { caseNoun, casePlural, checkRules, datasetOf, outputFields, ruleParts, taskRules } from '../../../lib/wizard';
import { frozenFile } from '../PlanSheet';
import { Button } from '../../ui/Button';
import { CardSection } from '../../ui/Card';
import { Sheet } from '../../ui/Sheet';

const fieldWords = (field: string) => {
  const words = field.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
};

/** A rule's text one sentence to a line, so a long rule reads in steps; the words stay the plan's own. */
export function ruleSentences(text: string): string[] {
  return text.split(/(?<=[.;])\s+(?=[A-Z"(])/).map((t) => t.trim()).filter(Boolean);
}

/** The task as written, for a plan with no "- " rule lines. */
function taskText(model: ForgeModel): string {
  return typeof model.plan?.data?.task === 'string' ? model.plan.data.task : '';
}

export function RulesSheet({ open, model, onClose, onChangePlan }: { open: boolean; model: ForgeModel; onClose: () => void; onChangePlan: () => void }) {
  const rules = taskRules(model);
  const checks = checkRules(model);
  const noun = caseNoun(model);
  const dataset = datasetOf(model);
  const frozen = frozenFile(model);
  const nullable = outputFields(model).some((f) => f.nullable);
  return (
    <Sheet
      open={open}
      onOpenChange={(o) => { if (!o) onClose(); }}
      title={rules.length ? `The ${rules.length} ${rules.length === 1 ? 'rule' : 'rules'}` : 'The job as written'}
      name="rules"
      footerStart={frozen ? undefined : <Button variant="outline" size="sm" data-action="change-plan" onClick={onChangePlan}>Edit plan</Button>}
      footer={<Button variant="primary" size="sm" data-action="done" onClick={onClose}>Done</Button>}
    >
      <p className="rules-intro">
        The agent follows {rules.length === 1 ? 'this' : 'these'} on every {noun}.
        {dataset ? ` ${rules.length === 1 ? 'It is' : 'They are'} written for the ${casePlural(noun)} in ${dataset.name}, so ${rules.length === 1 ? 'it uses' : 'they use'} the labels printed on them, in the ${casePlural(noun)}' own language.` : ''}
        {nullable ? ' Null means the field comes back empty.' : ''}
        {frozen ? ` The rules come from ${frozen}; to change them, edit that file.` : ''}
      </p>
      {rules.length ? (
        <ol className="rules-list" aria-label="Rules">
          {rules.map((rule, i) => {
            const { field, text } = ruleParts(rule);
            return (
              <li key={i}>
                {field && <b className="rules-field">{fieldWords(field)}</b>}
                {ruleSentences(text).map((sentence, j) => <span key={j} className="rules-sentence">{sentence}</span>)}
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="rules-prose">{taskText(model)}</p>
      )}
      {checks.length > 0 && (
        <CardSection title="Also checked by code on every answer" className="rules-checks-section">
          <p className="rules-intro">If the model gets one of these wrong, code corrects the answer before it is scored.</p>
          <ul className="rules-checks" aria-label="Checked by code">
            {checks.map((check, i) => <li key={i}>{check}</li>)}
          </ul>
        </CardSection>
      )}
    </Sheet>
  );
}
