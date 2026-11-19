// How a ride felt: reason tags (same lists as server/src/stezhka/feelings.py) and mark colours.
export const NEGATIVE_TAGS = ['surface', 'puddles', 'traffic', 'pedestrians', 'turns', 'steep', 'unsafe', 'other'];
export const POSITIVE_TAGS = ['smooth', 'quiet', 'scenic', 'green'];

/** Reasons offered for a feeling: problems for "didn't like", good things for "liked", both for "OK". */
export function tagsFor(feeling) {
  if (feeling === -1) return NEGATIVE_TAGS;
  if (feeling === 1) return POSITIVE_TAGS;
  return [...NEGATIVE_TAGS, ...POSITIVE_TAGS];
}

/** A mark's colour class: any problem wins, then good things, else a plain note. */
export function markKind(tags) {
  if (tags.some((t) => NEGATIVE_TAGS.includes(t))) return 'bad';
  if (tags.some((t) => POSITIVE_TAGS.includes(t))) return 'good';
  return 'note';
}

// Not the speed colours: marks are drawn as a wide halo under the track and as bands on the profile.
export const MARK_COLORS = { bad: '#e11d48', good: '#16a34a', note: '#0ea5e9', draft: '#7c3aed' };
