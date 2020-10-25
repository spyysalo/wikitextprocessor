# Definition of the processing context for Wikitext processing, and code for
# expanding templates, parser functions, and Lua macros.
#
# Copyright (c) 2020 Tatu Ylonen.  See file LICENSE and https://ylonen.org

import os
import re
import sys
import html
import pickle
import tempfile
import traceback
import collections
import html.entities
import multiprocessing
from .parserfns import (PARSER_FUNCTIONS, call_parser_function, tag_fn)
from .wikihtml import ALLOWED_HTML_TAGS
from .luaexec import call_lua_sandbox
from .parser import parse_encoded, NodeKind
from .common import (MAGIC_FIRST, MAGIC_LAST, MAX_MAGICS, MAGIC_NOWIKI_CHAR,
                     preprocess_text)
from .dumpparser import process_dump
from .node_expand import to_wikitext, to_html, to_text

# Set of HTML tags that need an explicit end tag.
PAIRED_HTML_TAGS = set(k for k, v in ALLOWED_HTML_TAGS.items()
                       if not v.get("no-end-tag"))

# Warning: this function is not re-entrant.  We store ctx and page_handler
# in global variables during dump processing, because they may not be
# pickleable.
_global_ctx = None
_global_page_handler = None

def phase2_page_handler(dt):
    """Helper function for calling the Phase2 page handler (see
    reprocess()).  This is a global function in order to make this
    pickleable.  The implication is that process() and reprocess() are not
    re-entrant (i.e., cannot be called safely from multiple threads or
    recursively)"""
    ctx = _global_ctx
    model, title = dt
    ctx.start_page(title)
    data = ctx.read_by_title(title)
    try:
        assert isinstance(data, str)
        ret = _global_page_handler(model, title, data)
        return True, ret
    except Exception as e:
        lst = traceback.format_exception(etype=type(e), value=e,
                                         tb=e.__traceback__)

        return False, "=== EXCEPTION:\n" + "".join(lst)


class Wtp(object):
    """Context used for processing wikitext and for expanding templates,
    parser functions and Lua macros.  The indended usage pattern is to
    initialize this context once (this holds template and module definitions),
    and then using the context for processing many pages."""
    __slots__ = (
        "buf",		 # Buffer for reading/writing tmp_file
        "buf_ofs",	 # Offset into buf
        "buf_used",	 # Number of bytes in the buffer when reading
        "buf_size",      # Allocated size of buf, in bytes
        "cache_file",	 # Prefix to cache files (instead of temporary file)
        "cache_file_old",  # Using pre-existing cache file
        "cookies",	 # Mapping from magic cookie -> expansion data
        "errors",	 # List of error messages (cleared for each new page)
        "fullpage",	 # The unprocessed text of the current page (or None)
        "lua",		 # Lua runtime or None if not yet initialized
        "lua_path",	 # Path to Lua modules
        "modules",	 # Lua code for defined Lua modules
        "need_pre_expand",  # Set of template names to be expanded before parse
        "num_threads",   # Number of parallel threads to use
        "page_contents",  # Full content for selected pages (e.g., Thesaurus)
        "page_seq",	 # All content pages (title, model, ofs, len) in order
        "quiet",	 # If True, don't print any messages during processing
        "redirects",	 # Redirects in the wikimedia project
        "rev_ht",	 # Mapping from text to magic cookie
        "expand_stack",	 # Saved stack before calling Lua function
        "templates",     # dict temlate name -> definition
        "title",         # current page title
        "tmp_file",	 # Temporary file used to store templates and pages
        "tmp_ofs",	 # Next write offset
        "warnings",	 # List of warning messages (cleared for each new page)
        # Data for parsing
        "beginning_of_line", # Parser at beginning of line
        "linenum",	 # Current line number
        "pre_parse",	 # XXX is pre-parsing still needed?
        "parser_stack",	 # Parser stack
        "suppress_special",  # XXX never set to True???
    )
    def __init__(self, quiet=False, num_threads=None, cache_file=None):
        self.buf_ofs = 0
        self.buf_size = 4 * 1024 * 1024
        self.buf = bytearray(self.buf_size)
        self.cache_file = cache_file
        self.cache_file_old = False
        self.cookies = []
        self.errors = []
        self.warnings = []
        self.lua = None
        self.quiet = quiet
        self.rev_ht = {}
        self.expand_stack = []
        self.num_threads = num_threads
        # Some predefined templates
        self.need_pre_expand = None

        # Open cache file if it exists; otherwise create new cache file or
        # temporary file and reset saved pages.
        self.tmp_file = None
        if self.cache_file:
            try:
                # Load self.templates, self.page_contents, self.page_seq,
                # self.redirects
                with open(self.cache_file + ".pickle", "rb") as f:
                    dt = pickle.load(f)
                version, dt = dt
                if version == 1:
                    # Cache file version is compatible
                    self.tmp_file = open(self.cache_file, "rb", buffering=0)
                    self.page_contents, self.page_seq, self.redirects, \
                        self.templates, self.need_pre_expand = dt
                    self.need_pre_expand = set(self.need_pre_expand)
                    self.cache_file_old = True
            except (FileNotFoundError, EOFError):
                pass
        if self.tmp_file is None:
            self._reset_pages()
        self.tmp_ofs = 0
        self.buf_ofs = 0

    def _reset_pages(self):
        """Resets any stored pages and gets ready to receive more pages."""
        self.tmp_file = None
        self.page_contents = {}
        self.page_seq = []
        self.redirects = {}
        self.templates = {}
        self.need_pre_expand = None
        self.cache_file_old = False
        # Add predefined templates
        self.templates["!"] = "&vert;"
        self.templates["%28%28"] = "&lbrace;&lbrace;"  # {{((}}
        self.templates["%29%29"] = "&rbrace;&rbrace;"  # {{))}}
        # Create cache file or temporary file
        if self.cache_file:
            # Create new cache file
            try:
                os.remove(self.cache_file)
            except FileNotFoundError:
                pass
            try:
                os.remove(self.cache_file + ".pickle")
            except FileNotFoundError:
                pass
            self.tmp_file = open(self.cache_file, "w+b", buffering=0)
        else:
            # Create temporary file
            self.tmp_file = tempfile.TemporaryFile(mode="w+b", buffering=0)

    def error(self, msg, trace=None):
        """Prints an error message to stdout.  The error is also saved in
        self.errors."""
        assert isinstance(msg, str)
        assert isinstance(trace, (str, type(None)))
        self.errors.append({"msg": msg, "trace": trace,
                            "path": tuple(self.expand_stack)})
        if self.expand_stack:
            msg += " at {}".format(self.expand_stack)
        if trace:
            msg += "\n" + trace
        print("{}: ERROR: {}".format(self.title, msg))
        sys.stdout.flush()

    def warning(self, msg, trace=None):
        """Prints a warning message to stdout.  The error is also saved in
        self.warnings."""
        assert isinstance(msg, str)
        assert isinstance(trace, (str, type(None)))
        self.warnings.append({"msg": msg, "trace": trace,
                              "path": tuple(self.expand_stack)})
        if self.expand_stack:
            msg += " at {}".format(self.expand_stack)
        if trace:
            msg += "\n" + trace
        print("{}: {}".format(self.title, msg))
        sys.stdout.flush()

    def _canonicalize_template_name(self, name):
        """Canonicalizes a template name by making its first character
        uppercase and replacing underscores by spaces and sequences of
        whitespace by a single whitespace."""
        assert isinstance(name, str)
        if name[:9] == "Template:":
            name = name[9:]
        name = re.sub(r"_", " ", name)
        name = re.sub(r"\s+", " ", name)
        name = re.sub(r"\(", "%28", name)
        name = re.sub(r"\)", "%29", name)
        name = re.sub(r"&", "%26", name)
        name = re.sub(r"\+", "%2B", name)
        name = name.strip()
        #if name:
        #    name = name[0].upper() + name[1:]
        return name


    def _canonicalize_parserfn_name(self, name):
        """Canonicalizes a parser function name by making its first character
        uppercase and replacing underscores by spaces and sequences of
        whitespace by a single whitespace."""
        assert isinstance(name, str)
        name = re.sub(r"_", " ", name)
        name = re.sub(r"\s+", " ", name)
        name = name.strip()
        if name not in PARSER_FUNCTIONS:
            name = name.lower()  # Parser function names are case-insensitive
        return name

    def _save_value(self, kind, args, nowiki):
        """Saves a value of a particular kind and returns a unique magic
        cookie for it."""
        assert kind in ("T", "A", "L")  # Template/parserfn, arg, link
        assert isinstance(args, (list, tuple))
        assert nowiki in (True, False)
        # print("save_value", kind, args, nowiki)
        args = tuple(args)
        v = (kind, args, nowiki)
        if v in self.rev_ht:
            return self.rev_ht[v]
        idx = len(self.cookies)
        if idx >= MAX_MAGICS:
            ctx.error("too many templates, arguments, or parser function calls")
            return ""
        self.cookies.append(v)
        ch = chr(MAGIC_FIRST + idx)
        self.rev_ht[v] = ch
        ret = ch
        return ret

    def _encode(self, text):
        """Encode all templates, template arguments, and parser function calls
        in the text, from innermost to outermost."""

        def repl_arg(m):
            """Replacement function for template arguments."""
            nowiki = m.group(0).find(MAGIC_NOWIKI_CHAR) >= 0
            orig = m.group(1)
            args = orig.split("|")
            return self._save_value("A", args, nowiki)

        def repl_templ(m):
            """Replacement function for templates {{name|...}} and parser
            functions."""
            nowiki = m.group(0).find(MAGIC_NOWIKI_CHAR) >= 0
            args = m.group(1).split("|")
            return self._save_value("T", args, nowiki)

        def repl_link(m):
            """Replacement function for links [[...]]."""
            nowiki = m.group(0).find(MAGIC_NOWIKI_CHAR) >= 0
            orig = m.group(1)
            return self._save_value("L", (orig,), nowiki)

        # As a preprocessing step, remove comments from the text
        text = re.sub(r"(?s)<!\s*--.*?--\s*>", "", text)

        # Main loop of encoding.  We encode repeatedly, always the innermost
        # template, argument, or parser function call first.  We also encode
        # links as they affect the interpretation of templates.
        while True:
            prev = text
            # Encode links.
            text = re.sub(r"\[" + MAGIC_NOWIKI_CHAR +
                          r"?\[(([^][{}]|\[[^][{}]*\])+)\]" +
                          # XXXremove: r"?\[(([^][{}]|\[[^]]*\])+)\]" +
                          MAGIC_NOWIKI_CHAR + r"?\]",
                          repl_link, text)
            # Encode template arguments.  We repeat this until there are
            # no more matches, because otherwise we could encode the two
            # innermost braces as a template transclusion.
            while True:
                prev2 = text
                text = re.sub(r"(?s)\{" + MAGIC_NOWIKI_CHAR +
                              r"?\{" + MAGIC_NOWIKI_CHAR +
                              r"?\{([^{}]*?)\}" +
                              MAGIC_NOWIKI_CHAR + r"?\}" +
                              MAGIC_NOWIKI_CHAR + r"?\}",
                              repl_arg, text)
                if text == prev2:
                    break
            # Handle template arguments that were INCORRECTLY closed by
            # a double brace instead of a triple brace.  There seem to be
            # annoyingly common.
            # while True:
            #     prev2 = text
            #     # Note: intentionally only two closing braces here!
            #     text = re.sub(r"(?s)\{" + MAGIC_NOWIKI_CHAR +
            #                   r"?\{" + MAGIC_NOWIKI_CHAR +
            #                   r"?\{([^{}]*?)\}" +
            #                   MAGIC_NOWIKI_CHAR + r"?\}",
            #                   repl_arg, text)
            #     # XXX should report error/warning, need separate repl function
            #     if text == prev2:
            #         break
            # Encode templates
            text = re.sub(r"(?s)\{" + MAGIC_NOWIKI_CHAR +
                          r"?\{(([^{}]|\}[^{}])+?)\}" +
                          MAGIC_NOWIKI_CHAR + r"?\}",
                          repl_templ, text)
            # We keep looping until there is no change during the iteration
            if text == prev:
                break
            prev = text
        # Replace any remaining braces etc by corresponding character entities
        #text = re.sub(r"\{([&|])", r"&lbrace;\1", text)
        #text = re.sub(r"\{([&|])", r"&lbrace;\1", text)
        #text = re.sub(r"[^|]\}", r"\1&rbrace;", text)
        #text = re.sub(r"[^|]\}", r"\1&rbrace;", text)
        #text = re.sub(r"\|", "&vert;", text)
        return text

    def _template_to_body(self, title, text):
        """Extracts the portion to be transcluded from a template body.  This
        returns an str."""
        assert isinstance(title, str)
        assert isinstance(text, str)
        # Remove all text inside <noinclude> ... </noinclude>
        text = re.sub(r"(?is)<\s*noinclude\s*>.*?<\s*/\s*noinclude\s*>",
                      "", text)
        text = re.sub(r"(?is)<\s*noinclude\s*/\s*>", "", text)
        # <onlyinclude> tags, if present, include the only text that will be
        # transcluded.  All other text is ignored.
        onlys = list(re.finditer(r"(?is)<\s*onlyinclude\s*>(.*?)"
                                 r"<\s*/\s*onlyinclude\s*>|"
                                 r"<\s*onlyinclude\s*/\s*>",
                                 text))
        if onlys:
            text = "".join(m.group(1) or "" for m in onlys)
        # Remove <includeonly>.  They mark text that is not visible on the page
        # itself but is included in transclusion.  Also text outside these tags
        # is included in transclusion.
        text = re.sub(r"(?is)<\s*(/\s*)?includeonly\s*(/\s*)?>", "", text)
        # Sanity checks for certain unbalanced tags.  However, it
        # appears some templates intentionally produce these and
        # intend them to be displayed.  Thus don't warn, and we may
        # even need to arrange for them to be properly parsed as text.
        if False:
           m = re.search(r"(?is)<\s*(/\s*)?noinclude\s*(/\s*)?>", text)
           if m:
               self.error("unbalanced {}".format(m.group(0)))
           m = re.search(r"(?is)<\s*(/\s*)?onlyinclude\s*(/\s*)?>", text)
           if m:
               self.error("unbalanced {}".format(m.group(0)))
        return text

    def add_page(self, model, title, text):
        """Collects information about the page.  For templates and modules,
        this keeps the content in memory.  For other pages, this saves the
        content in a temporary file so that it can be accessed later.  There
        must be enough space on the volume containing the temporary file
        to store the entire contents of the uncompressed WikiMedia dump.
        The content is saved because it is common for Wiktionary Lua macros
        to access content from arbitrary pages.  Furthermore, this way we
        only need to decompress and parse the dump file once.  ``model``
        is "wikitext" for normal pages, "redirect" for redirects (in which
        case ``text`` is the page pointed to), or "Scribunto" for Lua code;
        other values may also be encountered."""
        assert isinstance(model, str)
        assert isinstance(title, str)
        assert isinstance(text, str)

        # If we have previously analyzed pages and this is called again,
        # reset all previously saved pages (e.g., in case we are to update
        # existing cache file).
        if self.need_pre_expand is not None:
            self._reset_pages()

        # Save the page in our temporary file and metadata in memory
        rawtext = text.encode("utf-8")
        if self.buf_ofs + len(rawtext) > self.buf_size:
            bufview = memoryview(self.buf)[0: self.buf_ofs]
            self.tmp_file.write(bufview)
            self.buf_ofs = 0
        ofs = self.tmp_ofs
        self.tmp_ofs += len(rawtext)
        if len(rawtext) >= self.buf_ofs:
            self.tmp_file.write(rawtext)
        else:
            self.buf[self.buf_ofs: self.buf_ofs + len(rawtext)] = rawtext
        # XXX should we canonicalize title in page_contents
        self.page_contents[title] = (title, model, ofs, len(rawtext))
        self.page_seq.append((model, title))
        if not self.quiet and len(self.page_seq) % 10000 == 0:
            print("  ... {} raw pages collected"
                  .format(len(self.page_seq)))
            sys.stdout.flush()

        if model == "redirect":
            self.redirects[title] = text
            return
        if not title.startswith("Template:"):
            return
        if title.endswith("/documentation"):
            return
        if title.endswith("/testcases"):
            return

        # It is a template
        name = self._canonicalize_template_name(title)
        body = self._template_to_body(title, text)
        assert isinstance(body, str)
        self.templates[name] = body

    def _analyze_template(self, name, body):
        """Analyzes a template body and returns a set of the canonicalized
        names of all other templates it calls and a boolean that is True
        if it should be pre-expanded before final parsing and False if it
        need not be pre-expanded.  The pre-expanded flag is determined
        based on that body only; the caller should propagate it to
        templates that include the given template.  This does not work for
        template and template function calls where the name is generated by
        other expansions."""
        assert isinstance(body, str)
        included_templates = set()
        pre_expand = False

        # Determine if the template starts with a list item
        contains_list = re.match(r"(?s)^[#*;:]", body) is not None

        # Remove paired tables
        prev = body
        while True:
            unpaired_text = re.sub(
                r"(?s)(^|\n)\{\|([^\n]|\n+[^{|]|\n+\|[^}]|\n+\{[^|])*?\n+\|\}",
                r"", prev)
            if unpaired_text == prev:
                break
            prev = unpaired_text
        #print("unpaired_text {!r}".format(unpaired_text))

        # Determine if the template contains an unpaired table
        contains_unpaired_table = re.search(r"(?s)(^|\n)(\{\||\|\})",
                                            unpaired_text) is not None

        # Determine if the template contains table element tokens
        # outside paired table start/end.  We only try to look for
        # these outside templates, as it is common to write each
        # template argument on its own line starting with a "|".
        outside = unpaired_text
        while True:
            #print("=== OUTSIDE ITER")
            prev = outside
            while True:
                newt = re.sub(r"(?s)\{\{\{([^{}]|\}[^}]|\}\}[^}])*?\}\}\}",
                              "", prev)
                if newt == prev:
                    break
                prev = newt
            #print("After arg elim: {!r}".format(newt))
            newt = re.sub(r"(?s)\{\{([^{}]|\}[^}])*?\}\}", "", newt)
            #print("After templ elim: {!r}".format(newt))
            if newt == outside:
                break
            outside = newt
        # For now, we'll ignore !! and ||
        m = re.search(r"(?s)(^|\n)(\|\+|\|-|\||\!)", outside)
        contains_table_element = m is not None
        # if contains_table_element:
        #     print("contains_table_element {!r} at {}"
        #           .format(m.group(0), m.start()))
        #     print("... {!r} ...".format(outside[m.start() - 10:m.end() + 10]))
        #     print(repr(outside))

        # Check for unpaired HTML tags
        tag_cnts = collections.defaultdict(int)
        for m in re.finditer(r"(?si)<\s*(/\s*)?({})\b\s*[^>]*(/\s*)?>"
                             r"".format("|".join(PAIRED_HTML_TAGS)), outside):
            start_slash = m.group(1)
            tagname = m.group(2)
            end_slash = m.group(3)
            if start_slash:
                tag_cnts[tagname] -= 1
            elif not end_slash:
                tag_cnts[tagname] += 1
        contains_unbalanced_html = any(v != 0 for v in tag_cnts.values())
        # if contains_unbalanced_html:
        #     print(name, "UNBALANCED HTML")
        #     for k, v in tag_cnts.items():
        #         if v != 0:
        #             print("  {} {}".format(v, k))

        # Determine whether this template should be pre-expanded
        pre_expand = (contains_list or contains_unpaired_table or
                      contains_table_element or contains_unbalanced_html)

        # if pre_expand:
        #     print(name,
        #           {"list": contains_list,
        #            "unpaired_table": contains_unpaired_table,
        #            "table_element": contains_table_element,
        #            "unbalanced_html": contains_unbalanced_html,
        #            "pre_expand": pre_expand,
        #     })

        # Determine which other templates are called from unpaired text.
        # None of the flags we currently gather propagate outside a paired
        # table start/end.
        for m in re.finditer(r"(?s)(^|[^{])(\{\{)?\{\{([^{]*?)(\||\}\})",
                             unpaired_text):
            name = m.group(3)
            name = re.sub(r"(?si)<\s*nowiki\s*/\s*>", "", name)
            name = self._canonicalize_template_name(name)
            if not name:
                continue
            included_templates.add(name)

        return included_templates, pre_expand

    def analyze_templates(self):
        """Analyzes templates to determine which of them might create elements
        essential to parsing Wikitext syntax, such as table start or end
        tags.  Such templates generally need to be expanded before
        parsing the page."""
        self.need_pre_expand = set()
        included_map = collections.defaultdict(set)
        expand_q = []
        for name, body in self.templates.items():
            included_templates, pre_expand = self._analyze_template(name, body)
            for x in included_templates:
                included_map[x].add(name)
            if pre_expand:
                self.need_pre_expand.add(name)
                expand_q.append(name)

        # XXX consider encoding template bodies here (also need to save related
        # cookies).  This could speed up their expansion, where the first
        # operation is to encode them.  (Consider whether cookie numbers from
        # nested template expansions could conflict)

        # Propagate pre_expand from lower-level templates to all templates that
        # refer to them
        while expand_q:
            name = expand_q.pop()
            if name not in included_map:
                continue
            for inc in included_map[name]:
                if inc in self.need_pre_expand:
                    continue
                #print("propagating EXP {} -> {}".format(name, inc))
                self.need_pre_expand.add(inc)
                expand_q.append(name)

        # Copy template definitions to redirects to them
        for k, v in self.redirects.items():
            if not k.startswith("Template:"):
                # print("Unhandled redirect src", k)
                continue
            if not v.startswith("Template:"):
                # print("Unhandled redirect dst", v)
                continue
            k = self._canonicalize_template_name(k)
            v = self._canonicalize_template_name(v)
            if v not in self.templates:
                # print("{} redirects to non-existent template {}".format(k, v))
                continue
            if k in self.templates:
                # print("{} -> {} is redirect but already in templates"
                #       .format(k, v))
                continue
            self.templates[k] = self.templates[v]
            if v in self.need_pre_expand:
                self.need_pre_expand.add(k)

        # Save cache data
        if self.cache_file is not None and not self.cache_file_old:
            with open(self.cache_file + ".pickle", "wb") as f:
                pickle.dump((1, (self.page_contents, self.page_seq,
                                 self.redirects, self.templates,
                                 list(sorted(self.need_pre_expand)))),
                            f)

    def start_page(self, title):
        """Starts a new page for expanding Wikitext.  This saves the title and
        full page source in the context.  Calling this is mandatory for each
        page; expand_wikitext() can then be called multiple times for the same
        page.  This clears the self.errors and self.warnings lists."""
        assert isinstance(title, str)
        # variables and thus must be reloaded for each page.
        self.lua = None  # Force reloading modules for every page
        self.title = title
        self.errors = []
        self.warnings = []
        self.cookies = []
        self.rev_ht = {}
        self.expand_stack = [title]

    def _unexpanded_template(self, args, nowiki):
        """Formats an unexpanded template (whose arguments may have been
            partially or fully expanded)."""
        if nowiki:
            return ("&lbrace;&lbrace;" +
                    "&vert;".join(args) +
                    "&rbrace;&rbrace;")
        return "{{" + "|".join(args) + "}}"

    def _unexpanded_arg(self, args, nowiki):
        """Formats an unexpanded template argument reference."""
        if nowiki:
            return ("&lbrace;&lbrace;&lbrace;" +
                    "&vert;".join(args) +
                    "&rbrace;&rbrace;&rbrace;")
        return "{{{" + "|".join(args) + "}}}"

    def _unexpanded_link(self, args, nowiki):
        """Formats an unexpanded link."""
        if nowiki:
            return "&lsqb;&lsqb;" + "&vert;".join(args) + "&rsqb;&rsqb;"
        return "[[" + "|".join(args) + "]]"

    def expand(self, text, parent=None, pre_only=False,
               template_fn=None, post_template_fn=None,
               templates_to_expand=None,
               expand_parserfns=True, expand_invoke=True, quiet=False):
        """Expands templates and parser functions (and optionally Lua macros)
        from ``text`` (which is from page with title ``title``).
        ``templates_to_expand`` should be None to expand all
        templates, or a set or dictionary whose keys are those
        canonicalized template names that should be expanded.
        ``template_fn``, if given, will be used to expand templates;
        if it is not defined or returns None, the default expansion
        will be used (it can also be used to capture template
        arguments).  This returns the text with the given templates
        expanded."""
        assert isinstance(text, str)
        assert parent is None or (isinstance(parent, (list, tuple)) and
                                  len(parent) == 2)
        assert pre_only in (True, False)
        assert template_fn is None or callable(template_fn)
        assert post_template_fn is None or callable(post_template_fn)
        assert isinstance(templates_to_expand, (set, dict, type(None)))
        assert self.title is not None  # start_page() must have been called
        assert quiet in (False, True)

        # XXX implement post_template_fn(name, args_ht, expanded), to be
        # called after a template has been expanded

        # Handle <nowiki> in a preprocessing step
        text = preprocess_text(text)

        # If requesting to only pre_expand, then force templates to be expanded
        # to be those we detected as requiring pre-expansion
        if pre_only:
            if self.need_pre_expand is None:
                raise RuntimeError("analyze_templates() must be run first to "
                                   "determine which templates need pre-expand")
            templates_to_expand = self.need_pre_expand

        # If templates_to_expand is None, then expand all known templates
        if templates_to_expand is None:
            templates_to_expand = self.templates

        def invoke_fn(invoke_args, expander, parent):
            """This is called to expand a #invoke parser function."""
            assert isinstance(invoke_args, (list, tuple))
            assert callable(expander)
            assert isinstance(parent, (tuple, type(None)))
            # print("invoke_fn", invoke_args)
            # sys.stdout.flush()

            # Use the Lua sandbox to execute a Lua macro.  This will initialize
            # the Lua environment and store it in self.lua if it does not
            # already exist (it needs to be re-created for each new page).
            ret = call_lua_sandbox(self, invoke_args, expander, parent)
            return ret

        def expand(coded, parent, templates_to_expand):
            """This function does most of the work for expanding encoded
            templates, arguments, and parser functions."""
            assert isinstance(coded, str)
            assert isinstance(parent, (tuple, type(None)))
            assert isinstance(templates_to_expand, (set, dict))

            def expand_args(coded, argmap):
                assert isinstance(coded, str)
                assert isinstance(argmap, dict)
                parts = []
                pos = 0
                for m in re.finditer(r"[{:c}-{:c}]"
                                     .format(MAGIC_FIRST, MAGIC_LAST),
                                     coded):
                    new_pos = m.start()
                    if new_pos > pos:
                        parts.append(coded[pos:new_pos])
                    pos = m.end()
                    ch = m.group(0)
                    idx = ord(ch) - MAGIC_FIRST
                    kind, args, nowiki = self.cookies[idx]
                    assert isinstance(args, tuple)
                    if nowiki:
                        # If this template/link/arg has <nowiki />, then just
                        # keep it as-is (it won't be expanded)
                        parts.append(ch)
                        continue
                    if kind == "T":
                        # Template transclusion or parser function call.
                        # Expand its arguments.
                        new_args = tuple(map(lambda x: expand_args(x, argmap),
                                             args))
                        parts.append(self._save_value(kind, new_args, nowiki))
                        continue
                    if kind == "A":
                        # Template argument reference
                        if len(args) > 2:
                            self.warning("too many args ({}) in argument "
                                         "reference {!r}"
                                         .format(len(args), args))
                        self.expand_stack.append("ARG-NAME")
                        k = expand(expand_args(args[0], argmap),
                                   parent, self.templates).strip()
                        self.expand_stack.pop()
                        if k.isdigit():
                            k = int(k)
                        v = argmap.get(k, None)
                        if v is not None:
                            parts.append(v)
                            continue
                        if len(args) >= 2:
                            self.expand_stack.append("ARG-DEFVAL")
                            ret = expand_args(args[1], argmap)
                            self.expand_stack.pop()
                            parts.append(ret)
                            continue
                        # The argument is not defined (or name is empty)
                        arg = self._unexpanded_arg([str(k)], nowiki)
                        parts.append(arg)
                        continue
                    if kind == "L":
                        # Link to another page
                        content = args[0]
                        content = expand_args(content, argmap)
                        parts.append(self._unexpanded_link([content], nowiki))
                        continue
                    self.error("expand_arg: unsupported cookie kind {!r} in {}"
                               .format(kind, m.group(0)))
                    parts.append(m.group(0))
                parts.append(coded[pos:])
                return "".join(parts)

            def expand_parserfn(fn_name, args):
                if not expand_parserfns:
                    if not args:
                        return "{{" + fn_name + "}}"
                    return "{{" + fn_name + ":" + "|".join(args) + "}}"
                # Call parser function
                self.expand_stack.append(fn_name)
                expander = lambda arg: expand(arg, parent, self.templates)
                if fn_name == "#invoke":
                    if not expand_invoke:
                        return "{{#invoke:" + "|".join(args) + "}}"
                    ret = invoke_fn(args, expander, parent)
                else:
                    ret = call_parser_function(self, fn_name, args, expander)
                self.expand_stack.pop()  # fn_name
                # XXX if lua code calls frame:preprocess(), then we should
                # apparently encode and expand the return value, similarly to
                # template bodies (without argument expansion)
                # XXX current implementation of preprocess() does not match!!!
                return str(ret)

            # Main code of expand()
            parts = []
            pos = 0
            for m in re.finditer(r"[{:c}-{:c}]"
                                 .format(MAGIC_FIRST, MAGIC_LAST),
                                 coded):
                new_pos = m.start()
                if new_pos > pos:
                    parts.append(coded[pos:new_pos])
                pos = m.end()
                ch = m.group(0)
                idx = ord(ch) - MAGIC_FIRST
                kind, args, nowiki = self.cookies[idx]
                assert isinstance(args, tuple)
                if kind == "T":
                    if nowiki:
                        parts.append(self._unexpanded_template(args, nowiki))
                        continue
                    # Template transclusion or parser function call
                    # Limit recursion depth
                    if len(self.expand_stack) >= 100:
                        self.error("too deep recursion during template "
                                   "expansion")
                        parts.append(
                            '<strong class="error">too deep recursion '
                            'while expanding template {}</strong>'
                            .format(self._unexpanded_template(args, True)))
                        continue

                    # Expand template/parserfn name
                    self.expand_stack.append("TEMPLATE_NAME")
                    tname = expand(args[0], parent, templates_to_expand)
                    self.expand_stack.pop()

                    # Strip safesubst: and subst: prefixes
                    tname = tname.strip()
                    if tname[:10].lower() == "safesubst:":
                        tname = tname[10:]
                    elif tname[:6].lower() == "subst:":
                        tname = tname[6:]

                    # Check if it is a parser function call
                    ofs = tname.find(":")
                    if ofs > 0:
                        # It might be a parser function call
                        fn_name = self._canonicalize_parserfn_name(tname[:ofs])
                        # Check if it is a recognized parser function name
                        if (fn_name in PARSER_FUNCTIONS or
                            fn_name.startswith("#")):
                            ret = expand_parserfn(fn_name,
                                                  (tname[ofs + 1:].lstrip(),) +
                                                  args[1:])
                            parts.append(ret)
                            continue

                    # As a compatibility feature, recognize parser functions
                    # also as the first argument of a template (withoout colon),
                    # whether there are more arguments or not.  This is used
                    # for magic words and some parser functions have an implicit
                    # compatibility template that essentially does this.
                    fn_name = self._canonicalize_parserfn_name(tname)
                    if fn_name in PARSER_FUNCTIONS or fn_name.startswith("#"):
                        ret = expand_parserfn(fn_name, args[1:])
                        parts.append(ret)
                        continue

                    # Otherwise it must be a template expansion
                    name = tname
                    name = self._canonicalize_template_name(name)

                    # Check for undefined templates
                    if name not in self.templates:
                        if not quiet:
                            self.warning("undefined template {!r}"
                                         .format(tname))
                        parts.append(self._unexpanded_template(args, nowiki))
                        continue

                    # If this template is not one of those we want to expand,
                    # return it unexpanded (but with arguments possibly
                    # expanded)
                    if name not in templates_to_expand:
                        parts.append(self._unexpanded_template(args, nowiki))
                        continue

                    # Construct and expand template arguments
                    self.expand_stack.append(name)
                    ht = {}
                    num = 1
                    for i in range(1, len(args)):
                        arg = str(args[i])
                        m = re.match(r"""^\s*([^<>="']+?)\s*=\s*(.*?)\s*$""",
                                     arg)
                        if m:
                            # Note: Whitespace is stripped by the regexp
                            # around named parameter names and values per
                            # https://en.wikipedia.org/wiki/Help:Template
                            # (but not around unnamed parameters)
                            k, arg = m.groups()
                            if k.isdigit():
                                k = int(k)
                                if k < 1 or k > 1000:
                                    print("invalid argument number {}"
                                          .format(k))
                                    k = 1000
                                if num <= k:
                                    num = k + 1
                            else:
                                self.expand_stack.append("ARGNAME")
                                k = expand(k, parent, self.templates)
                                self.expand_stack.pop()
                        else:
                            k = num
                            num += 1
                        # Expand arguments in the context of the frame where
                        # they are defined.  This makes a difference for
                        # calls to #invoke within a template argument (the
                        # parent frame would be different).
                        self.expand_stack.append("ARGVAL-{}".format(k))
                        arg = expand(arg, parent, self.templates)
                        self.expand_stack.pop()
                        ht[k] = arg

                    # Expand the body, either using ``template_fn`` or using
                    # normal template expansion
                    t = None
                    if template_fn is not None:
                        t = template_fn(name, ht)
                    if t is None:
                        body = self.templates[name]
                        # XXX optimize by pre-encoding bodies during
                        # preprocessing
                        # (Each template is typically used many times)
                        # Determine if the template starts with a list item
                        contains_list = (re.match(r"(?s)^[#*;:]", body)
                                         is not None)
                        if contains_list:
                            body = "\n" + body
                        encoded_body = self._encode(body)
                        # Expand template arguments recursively.  The arguments
                        # are already expanded.
                        encoded_body = expand_args(encoded_body, ht)
                        # Expand the body using the calling template/page as
                        # the parent frame for any parserfn calls
                        new_title = tname.strip()
                        for prefix in ("Media", "Special", "Main", "Talk",
                                       "User",
                                       "User_talk", "Project", "Project_talk",
                                       "File", "Image", "File_talk",
                                       "MediaWiki", "MediaWiki_talk",
                                       "Template", "Template_talk",
                                       "Help", "Help_talk", "Category",
                                       "Category_talk", "Module",
                                       "Module_talk"):
                            if tname.startswith(prefix + ":"):
                                break
                        else:
                            new_title = "Template:" + new_title
                        new_parent = (new_title, ht)
                        # XXX no real need to expand here, it will expanded on
                        # next iteration anyway (assuming parent unchanged)
                        # Otherwise expand the body
                        t = expand(encoded_body, new_parent,
                                   templates_to_expand)

                    assert isinstance(t, str)
                    self.expand_stack.pop()  # template name
                    parts.append(t)
                elif kind == "A":
                    parts.append(self._unexpanded_arg(args, nowiki))
                elif kind == "L":
                    assert len(args) == 1
                    if nowiki:
                        parts.append(self._unexpanded_link(args, nowiki))
                    else:
                        # Link to another page
                        content = args[0]
                        self.expand_stack.append("[[link]]")
                        content = expand(content, parent, templates_to_expand)
                        self.expand_stack.pop()
                        parts.append(self._unexpanded_link([content], nowiki))
                else:
                    self.error("expand: unsupported cookie kind {!r} in {}"
                               .format(kind, m.group(0)))
                    parts.append(m.group(0))
            parts.append(coded[pos:])
            return "".join(parts)

        # Encode all template calls, template arguments, and parser function
        # calls on the page.  This is an inside-out operation.
        encoded = self._encode(text)

        # Recursively expand the selected templates.  This is an outside-in
        # operation.
        expanded = expand(encoded, parent, templates_to_expand)

        # Expand any remaining magic cookies and remove nowiki char
        expanded = self._finalize_expand(expanded, False)
        return expanded

    def _finalize_expand(self, text, unescape):
        """Expands any remaining magic characters (to their original values)
        and removes nowiki characters."""

        def magic_repl(m):
            idx = ord(m.group(0)) - MAGIC_FIRST
            kind, args, nowiki = self.cookies[idx]
            if kind == "T":
                return self._unexpanded_template(args, nowiki)
            if kind == "A":
                return self._unexpanded_arg(args, nowiki)
            if kind == "L":
                return self._unexpanded_link(args, nowiki)
            self.error("magic_repl: unsupported cookie kind {!r}"
                       .format(kind))
            return ""

        # Keep expanding magic cookies until they have all been expanded.
        # We might get them from, e.g., unexpanded_template()
        while True:
            prev = text
            text = re.sub(r"[{:c}-{:c}]".format(MAGIC_FIRST, MAGIC_LAST),
                          magic_repl, text)
            if prev == text:
                break

        # Unescape HTML entities if so requested (we don't do this at the end
        # of normal expansion, but we do it at the end of parsing)
        if unescape:
            text = html.unescape(text)

        # Remove the special <nowiki /> character
        text = re.sub(MAGIC_NOWIKI_CHAR, "", text)
        return text

    def process(self, path, page_handler, phase1_only=False):
        """Parses a WikiMedia dump file ``path`` (which should point to a
        "<project>-<date>-pages-articles.xml.bz2" file.  This calls
        ``page_handler(model, title, page)`` for each raw page.  This works
        in two phases - in the first phase this calls
        ctx.collect_specials() for each page to collect raw pages,
        especially templates and Lua modules.  Then this goes over the
        articles a second time, calling page_handler for each page
        (this automatically calls ctx.start_page(title) for each page
        before calling page_handler).  The page_handler will be called
        in parallel using the multiprocessing package, and thus it
        cannot save data in ``ctx`` or global variables.  It can only
        return its results.  This function will return a list
        containing all the results returned by page_handler (in
        arbirary order), except None values will be ignored.  This function
        is not re-entrant."""
        assert isinstance(path, str)
        assert callable(page_handler)
        return process_dump(self, path, page_handler, phase1_only)

    def reprocess(self, page_handler):
        """Reprocess all pages captured by self.process() or explicit calls
        to self.add_page().  This calls page_handler(model, title, text)
        for each page, and returns of list of their return values (ignoring
        None values).  This may call page_handler in parallel, and thus
        page_handler should not attempt to save anything between calls and
        should not modify global data.  This function is not re-entrant."""
        global _global_ctx
        global _global_page_handler
        _global_ctx = self
        _global_page_handler = page_handler

        if self.num_threads == 1:
            # Single-threaded version (without subprocessing).  This is
            # primarily intended for debugging.
            lst = []
            for model, title in self.page_seq:
                success, ret = phase2_page_handler((model, title))
                if not success:
                    print(ret)
                    continue
                if ret is not None:
                    lst.append(ret)
        else:
            # Process pages using multiple parallel processes (the normal
            # case)
            if self.num_threads is None:
                pool = multiprocessing.Pool()
            else:
                pool = multiprocessing.Pool(self.num_threads)
            lst = []
            for success, ret in pool.imap_unordered(phase2_page_handler,
                                                    self.page_seq):
                if not success:
                    print(ret)
                    continue
                if ret is not None:
                    lst.append(ret)
                    if not self.quiet and len(lst) % 1000 == 0:
                        print("  ... {}/{} pages ({:.1%}) processed"
                              .format(len(lst), len(self.page_seq),
                                      len(lst) / len(self.page_seq)))
                        sys.stdout.flush()
            pool.close()
            pool.join()
        return lst

    def page_exists(self, title):
        """Returns True if the given page exists, and False if it does not
        exist."""
        assert isinstance(title, str)
        # XXX should we canonicalize title?
        return title in self.page_contents

    def read_by_title(self, title):
        """Reads the contents of the page.  Returns None if the page does
        not exist."""
        assert isinstance(title, str)
        # XXX should we canonicalize title?
        if title not in self.page_contents:
            return None
        # The page seems to exist
        title, model, ofs, page_size = self.page_contents[title]
        # Use os.pread() so that we won't change the file offset; otherwise we
        # might cause a race condition with parallel scanning of the temporary
        # file.
        rawdata = os.pread(self.tmp_file.fileno(), page_size, ofs)
        return rawdata.decode("utf-8")

    def parse(self, text, pre_expand=False, expand_all=False):
        """Parses the given text into a parse tree (WikiNode tree).  If
        ``pre_expand`` is True, then before parsing this will expand those
        templates that have been detected to potentially influence the parsing
        results (e.g., they might produce table start or end or table rows).
        Likewise, if ``expand_all`` is True, this will expand all templates
        that have definitions (usually all of them).  Parser function calls
        and Lua macro invocations are expanded if they are inside expanded
        templates."""
        text = preprocess_text(text)

        # Expand some or all templates in the text as requested
        if expand_all:
            text = self.expand(text)
        elif pre_expand:
            text = self.expand(text, pre_only=True)

        # The Wikitext syntax is not context-free.  Also, tokenizing the
        # syntax properly does not seem to be possible without reference to
        # the overall structure.  We handle this with inside-out parsing
        # (which I haven't seen used elsewhere though it may have been).
        # The basic idea is that we replace template / template argument /
        # parser function call by a magic character, starting from the
        # innermost one, and then keep doing this until there is no more work
        # to do.  This allows us to disambiguate how braces group into
        # double and triple brace groups.  After the encoding, we do
        # a more traditional parsing of the rest, recursing into encoded parts.
        encoded = self._encode(text)
        root = parse_encoded(self, encoded)  # In parser.py
        return root

    def node_to_wikitext(self, node):
        """Converts the given parse tree node back to Wikitext."""
        return to_wikitext(node)

    def node_to_html(self, node, template_fn=None, post_template_fn=None):
        """Converts the given parse tree node to HTML."""
        return to_html(self, node, template_fn=template_fn,
                       post_template_fn=post_template_fn)

    def node_to_text(self, node, template_fn=None, post_template_fn=None):
        """Converts the given parse tree node to plain text."""
        return to_text(self, node, template_fn=template_fn,
                       post_template_fn=post_template_fn)

def phase1_to_ctx(pages):
    """Creates a context and adds the given pages to it.  THIS IS MOSTLY
    INTENDED FOR TESTS.  ``pages`` is a list or tuple of (tag, title, text),
    where ``tag`` is "Template" for templates and "Module" for modules.
    Title is the title of the page and text the content of the page."""
    ctx = Wtp()
    for tag, title, text in pages:
        ctx.add_page(tag, title, text)
    ctx.analyze_templates()
    return ctx
