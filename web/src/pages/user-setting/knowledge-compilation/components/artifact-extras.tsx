import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { useFieldArray, useFormContext } from 'react-hook-form';
import { useTranslation } from 'react-i18next';
import { TEXT_FIELD_MAX } from '../interface';
import { FieldListBlock } from './field-list-block';

interface ArtifactExtrasProps {
  /** Optional dot-path prefix (e.g. ``templates.0``) for use inside the
   * group-edit form. Defaults to '' for the single-template form. */
  pathPrefix?: string;
}

function _join(prefix: string | undefined, path: string): string {
  return prefix ? `${prefix}.${path}` : path;
}

/**
 * Claim + Concept blocks. Only mounted when `kind === 'artifacts'`.
 * Render-side conditional in the parent form keeps the React tree clean
 * when other kinds are selected.
 */
export function ArtifactExtras({ pathPrefix }: ArtifactExtrasProps) {
  // ``any`` widens the form context so the same section works under both
  // single-template and group (nested ``templates.N``) form schemas.
  const form = useFormContext<any>();
  const { t } = useTranslation();

  const claimFieldsPath = _join(pathPrefix, 'claim.fields');
  const conceptFieldsPath = _join(pathPrefix, 'concept.fields');

  const claimArray = useFieldArray({
    control: form.control,
    name: claimFieldsPath as any,
  });
  const conceptArray = useFieldArray({
    control: form.control,
    name: conceptFieldsPath as any,
  });

  return (
    <>
      {/* "Page-structure example" lives in the parent form, right
          under the LLM picker — see edit-template-form.tsx /
          child-form-section.tsx. Kept out of ArtifactExtras so the
          example sits visually above the Entity/Relation sections. */}
      <section className="flex flex-col gap-4 rounded-md border border-border-button p-4">
        <h3 className="text-base font-medium">
          {t('knowledgeCompilation.claimSpecification')}
        </h3>
        <FieldListBlock
          items={claimArray.fields}
          onAdd={() => claimArray.append({ statement: '', subject: '' })}
          onRemove={(index) => claimArray.remove(index)}
          addLabel={t('knowledgeCompilation.addField')}
          renderItem={(_item, index) => (
            <div className="flex flex-col gap-3">
              <FormField
                control={form.control}
                name={`${claimFieldsPath}.${index}.statement` as any}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-xs">
                      {t('knowledgeCompilation.statement')}
                    </FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        maxLength={TEXT_FIELD_MAX}
                        placeholder={t(
                          'knowledgeCompilation.statementPlaceholder',
                        )}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name={`${claimFieldsPath}.${index}.subject` as any}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-xs">
                      {t('knowledgeCompilation.subject')}
                    </FormLabel>
                    <FormControl>
                      <Textarea
                        {...field}
                        rows={2}
                        maxLength={TEXT_FIELD_MAX}
                        placeholder={t(
                          'knowledgeCompilation.subjectPlaceholder',
                        )}
                      />
                    </FormControl>
                    <p className="text-xs text-text-secondary text-right">
                      {(field.value ?? '').length}/{TEXT_FIELD_MAX}
                    </p>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
          )}
        />
      </section>

      <section className="flex flex-col gap-4 rounded-md border border-border-button p-4">
        <h3 className="text-base font-medium">
          {t('knowledgeCompilation.conceptSpecification')}
        </h3>
        <FieldListBlock
          items={conceptArray.fields}
          onAdd={() =>
            conceptArray.append({ term: '', definition_excerpt: '' })
          }
          onRemove={(index) => conceptArray.remove(index)}
          addLabel={t('knowledgeCompilation.addField')}
          renderItem={(_item, index) => (
            <div className="flex flex-col gap-3">
              <FormField
                control={form.control}
                name={`${conceptFieldsPath}.${index}.term` as any}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-xs">
                      {t('knowledgeCompilation.term')}
                    </FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        maxLength={TEXT_FIELD_MAX}
                        placeholder={t('knowledgeCompilation.termPlaceholder')}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name={`${conceptFieldsPath}.${index}.definition_excerpt` as any}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-xs">
                      {t('knowledgeCompilation.definitionExcerpt')}
                    </FormLabel>
                    <FormControl>
                      <Textarea
                        {...field}
                        rows={2}
                        maxLength={TEXT_FIELD_MAX}
                        placeholder={t(
                          'knowledgeCompilation.definitionExcerptPlaceholder',
                        )}
                      />
                    </FormControl>
                    <p className="text-xs text-text-secondary text-right">
                      {(field.value ?? '').length}/{TEXT_FIELD_MAX}
                    </p>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
          )}
        />
      </section>
    </>
  );
}
