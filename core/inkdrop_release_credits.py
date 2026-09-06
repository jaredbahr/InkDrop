"""One closed vocabulary of release-group credits.

Three judgements ask the same question of a release name -- is this trailing
text the scanner's credit, or another work's name? -- and until now each
carried its own answer. The indexer classifier knew twenty-four phrases, the
matcher's singleton path five, the artifact acceptor fourteen, and they
drifted: a one-shot whose release the classifier had just cleared of a
"danke-Empire" credit was parked in manual review by the matcher for the same
credit, because the matcher had never heard of it. One list, read by all
three, so a credit learned once is known everywhere.

The set stays closed. Whole tuples, never a token soup: an unknown trailing
word is still identity-bearing in every reader, and a credit joins here only
when a real release has shown it.
"""

# Multi-word credits, as they tokenize. "Son of Ultron-Empire", "Zone-Empire",
# "Minutemen-DTs" and the rest are scanner collectives, not works.
RELEASE_GROUP_PHRASES = frozenset(
    {
        ("f", "son", "of", "ultron", "empire"),
        ("son", "of", "ultron", "empire"),
        ("f", "archangel", "zone", "empire"),
        ("archangel", "zone", "empire"),
        ("zone", "empire"),
        ("zerodaze", "dcp", "hd"),
        ("minutemen", "phd"),
        ("lostnerevarine", "empire"),
        ("danke", "empire"),
        ("darkness", "empire"),
        ("darkzone", "empire"),
        ("kileko", "empire"),
        ("kingpin", "empire"),
        ("mango", "empire"),
        ("mephisto", "empire"),
        ("phillywilly", "empire"),
        ("shan", "empire"),
        ("xra", "empire"),
        ("minutemen", "dts"),
        ("steam", "dcp"),
        ("theproletariat", "dcp"),
    }
)

# Publisher names that turn up as release metadata. They are not credits and
# never vouch for a file on their own; the indexer classifier consumes them as
# leading or trailing metadata and nothing else does.
PUBLISHER_PHRASES = frozenset(
    {
        ("dc", "comics"),
        ("marvel", "comics"),
        ("image", "comics"),
    }
)

# One-word handles with no shape of their own -- nothing but the name says
# "(1r0n)" is a scanner and "(Conan)" is a book. Read only where a credit can
# sit: the last thing in a name, or inside its own parentheses.
RELEASE_GROUP_HANDLES = frozenset({"1r0n", "jko", "lucaz", "oda", "rillant", "shizu"})

# What may close a release name and nothing else: the handles, and a bare
# "Empire" -- "digital-Empire" is the commonest credit on Usenet and the word
# is metadata there, while "Star Wars: Empire 001" is a different comic. The
# position is the whole distinction; readers apply it at the end of a name.
TRAILING_RELEASE_CREDITS = frozenset({(handle,) for handle in RELEASE_GROUP_HANDLES} | {("empire",)})
