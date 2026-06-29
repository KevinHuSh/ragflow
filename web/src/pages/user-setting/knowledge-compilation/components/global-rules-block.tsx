import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Textarea } from '@/components/ui/textarea';
import { useFormContext } from 'react-hook-form';
import { useTranslation } from 'react-i18next';
import { GLOBAL_RULES_FIELD_MAX } from '../interface';

interface GlobalRulesBlockProps {
  /** Optional dot-path prefix (e.g. ``templates.0``) used when this
   * block is rendered inside the group-edit form. */
  pathPrefix?: string;
}

/**
 * Global compilation rules — a single capped textarea.
 * Distinguished from per-field rules: this section applies to the whole
 * extraction, not to one entity/relation type.
 */
export function GlobalRulesBlock({ pathPrefix }: GlobalRulesBlockProps) {
  // See {@link ArtifactExtras} for why the form-context type is widened.
  const form = useFormContext<any>();
  const { t } = useTranslation();

  const rulesPath = pathPrefix ? `${pathPrefix}.global_rules` : 'global_rules';

  return (
    <FormField
      control={form.control}
      name={rulesPath as any}
      render={({ field }) => (
        <FormItem>
          <FormLabel>{t('knowledgeCompilation.globalRules')}</FormLabel>
          <FormControl>
            <Textarea
              {...field}
              maxLength={GLOBAL_RULES_FIELD_MAX}
              rows={4}
              placeholder={t('knowledgeCompilation.globalRulesPlaceholder')}
            />
          </FormControl>
          <p className="text-xs text-text-secondary text-right">
            {(field.value ?? '').length}/{GLOBAL_RULES_FIELD_MAX}
          </p>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}
