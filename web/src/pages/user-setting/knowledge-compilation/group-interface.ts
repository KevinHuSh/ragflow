import {
  CompilationTemplate,
  CompilationTemplateGroup,
} from '@/interfaces/database/compilation-template';
import { z } from 'zod';
import {
  CompilationTemplateFormValues,
  compilationTemplateFormSchema,
  emptyFormValues,
  formValuesToTemplateConfig,
  templateConfigToFormValues,
} from './interface';

const NAME_MAX = 128;
const DESCRIPTION_MAX = 1024;

/**
 * Zod schema for the group editor. ``templates`` is the foldable
 * children list — each entry reuses the per-template form schema.
 * Cross-template invariant (artifacts-vs-rest mutual exclusion) is
 * enforced in ``superRefine`` so the user gets a single specific error
 * message rather than a generic "form invalid" toast.
 */
export const compilationTemplateGroupFormSchema = z
  .object({
    name: z.string().trim().min(1, 'Group name is required.').max(NAME_MAX),
    description: z.string().max(DESCRIPTION_MAX).optional().default(''),
    templates: z
      .array(compilationTemplateFormSchema)
      .min(1, 'A group must contain at least one template.'),
  })
  .superRefine((values, ctx) => {
    const kinds = values.templates.map((t) => t.kind);
    const artifactCount = kinds.filter((k) => k === 'artifacts').length;
    if (artifactCount > 0 && values.templates.length > 1) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          'An artifacts template cannot be combined with other templates in the same group.',
        path: ['templates'],
      });
    }
    // At most one tree-kind child may enable re-chunking — multiple
    // would race on the same source chunks during the soft-delete pass.
    // Mirrors the backend invariant in
    // ``_enforce_single_rechunk_tree``.
    const rechunkTrees = values.templates.filter(
      (t) => t.kind === 'tree' && t.raptor?.rechunk === true,
    );
    if (rechunkTrees.length > 1) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Only one tree template in a group may enable re-chunking.',
        path: ['templates'],
      });
    }
    // Children are no longer named — uniqueness is implicit in their
    // (kind, index) tuple, and the group as a whole carries the
    // human-facing name.
  });

export type CompilationTemplateGroupFormValues = z.infer<
  typeof compilationTemplateGroupFormSchema
>;

/**
 * Derive the scope client-side so we can render a badge without a
 * round-trip. The backend enforces the same rule on save.
 */
export function deriveGroupScope(
  templates: Pick<CompilationTemplateFormValues, 'kind'>[],
): 'dataset' | 'file' {
  return templates.some((t) => t.kind === 'artifacts') ? 'dataset' : 'file';
}

/**
 * Seed an empty group form — one blank ``empty``-kind child so the user
 * has something to expand and configure on first open.
 */
export function emptyGroupFormValues(): CompilationTemplateGroupFormValues {
  return {
    name: '',
    description: '',
    templates: [emptyFormValues()],
  };
}

/**
 * Translate a server-side group into the form's value shape.
 */
export function groupToFormValues(
  group: CompilationTemplateGroup,
): CompilationTemplateGroupFormValues {
  return {
    name: group.name,
    description: group.description ?? '',
    templates: (group.templates ?? []).map((t) =>
      templateConfigToFormValues(t.description, t.config),
    ),
  };
}

/**
 * Translate the form's value shape into the create/update request body.
 * Children are emitted in the same order the user arranged them.
 */
export function groupFormValuesToPayload(
  values: CompilationTemplateGroupFormValues,
) {
  return {
    name: values.name.trim(),
    description: values.description || '',
    // Per-child ``name`` is no longer collected — the backend derives a
    // placeholder (``${kind}_${index+1}``) when persisting so the DB's
    // NOT NULL column stays satisfied without forcing the user to
    // name each child.
    templates: values.templates.map((t) => ({
      description: t.description || '',
      kind: t.kind,
      config: formValuesToTemplateConfig(t),
    })),
  };
}

/**
 * Pluck one child from a saved group when the user wants to edit just
 * that child's section in isolation. Not currently used by the UI but
 * handy if we ever want a sub-dialog flow.
 */
export function childTemplateFromGroup(
  group: CompilationTemplateGroup,
  index: number,
): CompilationTemplate | undefined {
  return group.templates?.[index];
}
