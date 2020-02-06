from cravat import BaseConverter
from cravat import BadFormatError
import cravat.constants as constants
from pyliftover import LiftOver
import os
from cravat.exceptions import LiftoverFailure
from cravat.inout import CravatWriter

class CravatConverter(BaseConverter):
    comp_base = {'A':'T','T':'A','C':'G','G':'C','-':'-','N':'N'}
    
    def __init__(self):
        self.format_name = 'ancestrydna'
    
    def check_format(self, f):
        return 'AncestryDNA' in f.readline()
    
    def setup(self, f):
        self.lifter = LiftOver(constants.liftover_chain_paths['hg19'])
        fprefix = 'GCA_000001405.15_GRCh38_no_alt_analysis_set'
        prefix = os.path.join(os.path.dirname(os.path.abspath(__file__)),'data',fprefix)
        self.btr = BowtieIndexReference(prefix)
        ex_info_fpath = os.path.join(self.output_dir, self.run_name + '.extra_variant_info.var')
        self.ex_info_writer = CravatWriter(ex_info_fpath)
        cols = [
            constants.crv_def[0],
            {
                'name': 'zygosity',
                'title': 'Zygosity',
                'type': 'string',
                'category': 'single',
                'categories': ['het','hom'],
            }
        ]
        self.ex_info_writer.add_columns(cols)
        self.ex_info_writer.write_definition()
        self.ex_info_writer.write_meta_line('name', 'extra_variant_info')
        self.ex_info_writer.write_meta_line('displayname', 'Extra Variant Info')
        self.cur_zygosity = None

    def convert_line(self, l):
        ret = []
        if l.startswith('#'): return self.IGNORE
        if l.startswith('rsid'): return self.IGNORE
        toks = l.strip('\r\n').split('\t')
        tags = toks[0]
        chrom = toks[1]
        chromint = int(chrom)
        if chromint == 23:
            chrom = 'X'
        elif chromint==24 or chromint==25:
            chrom = 'Y'
        elif chromint == 26:
            chrom = 'M'
        chrom = 'chr'+chrom
        pos = toks[2]
        hg38_coords = self.lifter.convert_coordinate(chrom, int(pos))
        if hg38_coords != None and len(hg38_coords) > 0:
            chrom38 = hg38_coords[0][0]
            pos38 = hg38_coords[0][1]      
            ref = self.btr.get_stretch(chrom38, pos38-1, 1)
        else:
            raise(LiftoverFailure('Liftover failure'))      
        sample = ''
        good_vars = set(['T','C','G','A'])
        try:
            if toks[3]==toks[4]:
                self.cur_zygosity = 'hom'
            else:
                self.cur_zygosity = 'het'
        except IndexError:
            self.cur_zygosity = 'hom'

        for var in toks[3:]:
            if var in good_vars:
                alt = var
                wdict = {'tags':tags,
                    'chrom':chrom,
                    'pos':pos,
                    'ref_base':ref,
                    'alt_base':alt,
                    'sample_id':sample}
                ret.append(wdict)
        return ret

    def addl_operation_for_unique_variant (self, wdict, wdict_no):
        uid = wdict['uid']
        row_data = {
            'uid': wdict['uid'],
            'zygosity': self.cur_zygosity,
        }
        self.ex_info_writer.write_data(row_data)

    def cleanup(self):
        self.ex_info_writer.close()

###################################################################################################
"""
bowtie_index.py

Includes class for grabbing genome sequence from Bowtie index.

The MIT License (MIT)
Taken from Rail-RNA, which is copyright (c) 2015 
                    Abhinav Nellore, Leonardo Collado-Torres,
                    Andrew Jaffe, James Morton, Jacob Pritt,
                    José Alquicira-Hernández,
                    Christopher Wilks,
                    Jeffrey T. Leek, and Ben Langmead.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import os
import struct
import mmap
from operator import itemgetter
from collections import defaultdict
from bisect import bisect_right
import sys

if sys.version_info[0] > 2:

    def byte_readline(fh):
        return fh.readline().decode(encoding="UTF-8")

    # we need a version of ord that does nothing in python 3
    # but regular ord is still needed on refname reads thanks to
    # our byte_readline
    def ord2or3(chr):
        return chr


else:

    def byte_readline(fh):
        return fh.readline()

    def ord2or3(chr):
        return ord(chr)


class BowtieIndexReference(object):
    """
    Given prefix of a Bowtie index, parses the reference names, parses the
    extents of the unambiguous stretches, and memory-maps the file containing
    the unambiguous-stretch sequences.  get_stretch member function can
    retrieve stretches of characters from the reference, even if the stretch
    contains ambiguous characters.
    """

    def __init__(self, idx_prefix):

        # Open file handles
        if os.path.exists(idx_prefix + ".3.ebwt"):
            # Small index (32-bit offsets)
            fh1 = open(idx_prefix + ".1.ebwt", "rb")  # for ref names
            fh3 = open(idx_prefix + ".3.ebwt", "rb")  # for stretch extents
            fh4 = open(idx_prefix + ".4.ebwt", "rb")  # for unambiguous sequence
            sz, struct_unsigned = 4, struct.Struct("I")
        else:
            raise RuntimeError('No Bowtie index files with prefix "%s"' % idx_prefix)

        #
        # Parse .1.bt2 file
        #
        one = struct.unpack("<i", fh1.read(4))[0]
        assert one == 1

        ln = struct_unsigned.unpack(fh1.read(sz))[0]
        line_rate = struct.unpack("<i", fh1.read(4))[0]
        lines_per_side = struct.unpack("<i", fh1.read(4))[0]
        _ = struct.unpack("<i", fh1.read(4))[0]
        ftab_chars = struct.unpack("<i", fh1.read(4))[0]
        _ = struct.unpack("<i", fh1.read(4))[0]

        nref = struct_unsigned.unpack(fh1.read(sz))[0]
        # get ref lengths
        reference_length_list = []
        for i in range(nref):
            reference_length_list.append(struct.unpack("<i", fh1.read(sz))[0])

        nfrag = struct_unsigned.unpack(fh1.read(sz))[0]
        # skip rstarts
        fh1.seek(nfrag * sz * 3, 1)

        # skip ebwt
        bwt_sz = ln // 4 + 1
        line_sz = 1 << line_rate
        side_sz = line_sz * lines_per_side
        side_bwt_sz = side_sz - 8
        num_side_pairs = (bwt_sz + (2 * side_bwt_sz) - 1) // (2 * side_bwt_sz)
        ebwt_tot_len = num_side_pairs * 2 * side_sz
        fh1.seek(ebwt_tot_len, 1)

        # skip zOff
        fh1.seek(sz, 1)

        # skip fchr
        fh1.seek(5 * sz, 1)

        # skip ftab
        ftab_len = (1 << (ftab_chars * 2)) + 1
        fh1.seek(ftab_len * sz, 1)

        # skip eftab
        eftab_len = ftab_chars * 2
        fh1.seek(eftab_len * sz, 1)

        refnames = []
        while True:
            refname = byte_readline(fh1)
            if len(refname) == 0 or ord(refname[0]) == 0:
                break
            refnames.append(refname.split()[0])
        assert len(refnames) == nref

        #
        # Parse .3.bt2 file
        #
        one = struct.unpack("<i", fh3.read(4))[0]
        assert one == 1

        nrecs = struct_unsigned.unpack(fh3.read(sz))[0]

        running_unambig, running_length = 0, 0
        self.recs = defaultdict(list)
        self.offset_in_ref = defaultdict(list)
        self.unambig_preceding = defaultdict(list)
        length = {}

        ref_id, ref_namenrecs_added = 0, None
        for i in range(nrecs):
            off = struct_unsigned.unpack(fh3.read(sz))[0]
            ln = struct_unsigned.unpack(fh3.read(sz))[0]
            first_of_chromosome = ord(fh3.read(1)) != 0
            if first_of_chromosome:
                if i > 0:
                    length[ref_name] = running_length
                ref_name = refnames[ref_id]
                ref_id += 1
                running_length = 0
            assert ref_name is not None
            self.recs[ref_name].append((off, ln, first_of_chromosome))
            self.offset_in_ref[ref_name].append(running_length)
            self.unambig_preceding[ref_name].append(running_unambig)
            running_length += off + ln
            running_unambig += ln

        length[ref_name] = running_length
        assert nrecs == sum(map(len, self.recs.values()))

        #
        # Memory-map the .4.bt2 file
        #
        ln_bytes = (running_unambig + 3) // 4
        self.fh4mm = mmap.mmap(fh4.fileno(), ln_bytes, access=mmap.ACCESS_READ)

        # These are per-reference
        self.length = length
        self.refnames = refnames

        # To facilitate sorting reference names in order of descending length
        sorted_rnames = sorted(
            self.length.items(), key=lambda x: itemgetter(1)(x), reverse=True
        )
        """A case-sensitive sort is also necessary here because new versions of
        bedGraphToBigWig complain on encountering a nonlexicographic sort
        order."""
        lexicographically_sorted_rnames = sorted(
            self.length.items(), key=lambda x: itemgetter(0)(x)
        )
        self.rname_to_string, self.l_rname_to_string = {}, {}
        self.string_to_rname, self.l_string_to_rname = {}, {}
        for i, (rname, _) in enumerate(sorted_rnames):
            rname_string = "%012d" % i
            self.rname_to_string[rname] = rname_string
            self.string_to_rname[rname_string] = rname
        for i, (rname, _) in enumerate(lexicographically_sorted_rnames):
            rname_string = "%012d" % i
            self.l_rname_to_string[rname] = rname_string
            self.l_string_to_rname[rname_string] = rname
        # Handle unmapped reads
        unmapped_string = "%012d" % len(sorted_rnames)
        self.rname_to_string["*"] = unmapped_string
        self.string_to_rname[unmapped_string] = "*"

        # For compatibility
        self.rname_lengths = self.length
        fh1.close()
        fh3.close()
        fh4.close()

    def get_stretch(self, ref_id, ref_off, count):
        """
        Return a stretch of characters from the reference, retrieved
        from the Bowtie index.

        @param ref_id: name of ref seq, up to & excluding whitespace
        @param ref_off: offset into reference, 0-based
        @param count: # of characters
        @return: string extracted from reference
        """
        # Account for negative reference offsets by padding with Ns
        N_count = min(abs(min(ref_off, 0)), count)
        stretch = ["N"] * N_count
        count -= N_count
        if not count:
            return "".join(stretch)
        ref_off = max(ref_off, 0)
        starting_rec = bisect_right(self.offset_in_ref[ref_id], ref_off) - 1
        off = self.offset_in_ref[ref_id][starting_rec]
        buf_off = self.unambig_preceding[ref_id][starting_rec]
        # Naive to scan these records linearly; obvious speedup is binary search
        for rec in self.recs[ref_id][starting_rec:]:
            off += rec[0]
            while ref_off < off and count > 0:
                stretch.append("N")
                count -= 1
                ref_off += 1
            if count == 0:
                break
            if ref_off < off + rec[1]:
                # stretch extends through part of the unambiguous stretch
                buf_off += ref_off - off
            else:
                buf_off += rec[1]
            off += rec[1]
            while ref_off < off and count > 0:
                buf_elt = buf_off >> 2
                shift_amt = (buf_off & 3) << 1
                stretch.append("ACGT"[(ord2or3(self.fh4mm[buf_elt]) >> shift_amt) & 3])
                buf_off += 1
                count -= 1
                ref_off += 1
            if count == 0:
                break
        # If the requested stretch went past the last unambiguous
        # character in the chromosome, pad with Ns
        while count > 0:
            count -= 1
            stretch.append("N")
        return "".join(stretch)


def which(program):
    def is_exe(fp):
        return os.path.isfile(fp) and os.access(fp, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None


if __name__ == "__main__":

    import sys
    import unittest
    import argparse
    from tempfile import mkdtemp
    from shutil import rmtree

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", action="store_const", const=True, default=False, help="Do unit tests"
    )

    args = parser.parse_args()

    if args.test:
        import unittest

        class TestBowtieIndexReference(unittest.TestCase):
            def setUp(self):
                self.tmpdir = mkdtemp()
                self.fa_fn_1 = os.path.join(self.tmpdir, "tmp1.fa")
                with open(self.fa_fn_1, "w") as fh:
                    fh.write(
                        """>short_name1 with some stuff after whitespace
ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT
ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT
A
>short_name4
NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN
CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC
NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN
GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG
NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN
TT
>short_name2 with some stuff after whitespace
CAGTCAGTCAGTCAGTCAGTCAGTCAGTCAGTCAGTCAGT
CAGTCAGTCAGTCAGTCAGTCAGTCAGTCAGTCAGTCAGT
>short_name3 with some stuff after whitespace
CA
"""
                    )
                assert which("bowtie-build") is not None
                os.system(
                    "bowtie-build %s %s >/dev/null" % (self.fa_fn_1, self.fa_fn_1)
                )

            def tearDown(self):
                rmtree(self.tmpdir)

            def test1(self):
                ref = BowtieIndexReference(self.fa_fn_1)
                self.assertEqual("ACGTACGTAC", ref.get_stretch("short_name1", 0, 10))
                self.assertEqual("ACGTACGTAC", ref.get_stretch("short_name1", 40, 10))
                self.assertEqual("ANNNNNNNNN", ref.get_stretch("short_name1", 80, 10))

                self.assertEqual("CAGTCAGTCA", ref.get_stretch("short_name2", 0, 10))
                self.assertEqual("CAGTCAGTCA", ref.get_stretch("short_name2", 40, 10))
                self.assertEqual("NNNNNNNNNN", ref.get_stretch("short_name2", 80, 10))

                self.assertEqual("CANNNNNNNN", ref.get_stretch("short_name3", 0, 10))

            def test2(self):
                ref = BowtieIndexReference(self.fa_fn_1)
                self.assertEqual("CAGTCAGTCA", ref.get_stretch("short_name2", 0, 10))
                self.assertEqual("AGTCAGTCAGT", ref.get_stretch("short_name2", 1, 11))
                self.assertEqual("GTCAGTCAGTCA", ref.get_stretch("short_name2", 2, 12))
                self.assertEqual("TCAGTCAGTCAGT", ref.get_stretch("short_name2", 3, 13))

                self.assertEqual("TACGTACGTA", ref.get_stretch("short_name1", 71, 10))
                self.assertEqual("ACGTACGTANN", ref.get_stretch("short_name1", 72, 11))
                self.assertEqual("CGTACGTANNNN", ref.get_stretch("short_name1", 73, 12))

            def test3(self):
                ref = BowtieIndexReference(self.fa_fn_1)
                self.assertEqual(
                    "NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN",
                    ref.get_stretch("short_name4", 0, 40),
                )
                self.assertEqual(
                    "NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNA",
                    ref.get_stretch("short_name4", 1, 40),
                )
                self.assertEqual("AAAA", ref.get_stretch("short_name4", 41, 4))
                self.assertEqual(
                    "NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNTT",
                    ref.get_stretch("short_name4", 240, 42),
                )

            def test4(self):
                ref = BowtieIndexReference(self.fa_fn_1)
                # Test that all refname lengths are accurate
                self.assertEqual(ref.length["short_name1"], 81)
                self.assertEqual(ref.length["short_name4"], 282)
                self.assertEqual(ref.length["short_name2"], 80)
                self.assertEqual(ref.length["short_name3"], 2)

            def test_off_reference_values(self):
                ref = BowtieIndexReference(self.fa_fn_1)
                self.assertEqual("NNNACG", ref.get_stretch("short_name1", -3, 6))
                self.assertEqual("NNNNN", ref.get_stretch("short_name1", -20, 5))
                self.assertEqual("NNNNNNNNN", ref.get_stretch("short_name1", 85, 9))
                self.assertEqual("ANNNNNNNN", ref.get_stretch("short_name1", 80, 9))

        unittest.main(argv=[sys.argv[0]])
