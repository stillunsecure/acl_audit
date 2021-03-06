from netlib import odict
from mitmproxy import dump, flow, controller, proxy
from flow_plugin import FlowResult, FlowPlugin
from group_plugin import Group
from plugin import Plugin

import os
import re
import xlsxwriter


class RecordMaster(controller.Master):

    def __init__(self, options):
        self.options = options
        replay_file = 'output/{0}'.format(options.replay_file)
        self.tmp_file = open(replay_file, 'wb')
        self.writer = flow.FlowWriter(self.tmp_file)
        config = proxy.ProxyConfig(port=int(options.listen_port), http2=False)
        server = proxy.ProxyServer(config)
        print 'Listening on {0}'.format(server.address)
        super(RecordMaster, self).__init__(server)

    def handle_response(self, f):
        if re.search(self.options.host, f.request.host):
            if 'strict-transport-security' in f.response.headers:
                f.response.headers['strict-transport-security'] = 'max-age=0;'
            self.filter(f)
        else:
            print
        f.reply()

    def filter(self, f):

        content_type = ''

        if 'content-type' in f.response.headers:
            content_type = f.response.headers['content-type']

        rec_output = '{0}, content-type = {1} -> {2} {3}'.format(f.request.host, content_type, f.request.method, f.request.path)
        if re.search(self.options.record_content_type, content_type) and \
               re.search(self.options.record_uri_filter, f.request.path):
            print 'Recording {0}'.format(rec_output)
            self.writer.add(f)
        elif self.options.show_ignored == True:
            print 'Ignoring {0}'.format(rec_output)


class AuditMaster(dump.DumpMaster):

    ManualCookieMode = 'manual'
    RecordedCookieMode = 'recorded'
    RemoveAllCookieMode = 'removeall'

    def __init__(self,
                 group_plugin,
                 flow_plugin,
                 host,
                 replay_file,
                 cookie_mode):
        config = proxy.ProxyConfig(port=int(8080), http2=False)
        server = proxy.DummyServer(config)
        opts = dump.Options()
        opts.anticache = True
        opts.flow_detail = 0
        replay_file = 'output/{0}'.format(replay_file)
        opts.client_replay = [replay_file]
        super(AuditMaster, self).__init__(server, opts)

        self.host = host
        self.results = []
        self.group_plugin = group_plugin
        self.flow_plugin = flow_plugin
        self.flow_plugin.current_flow_id = 0
        self.cookies = {}
        self.cookie_mode = cookie_mode

    def run(self):
        self.flow_plugin.start_session()
        super(AuditMaster, self).run()

    def handle_request(self, f):
        print '\tReplaying {0}'.format(f.request.path)

        if 'cookie' in f.request.headers:
            f.request.cookies = odict.ODict()
            f.request.headers.pop('cookie')

        if self.cookie_mode == AuditMaster.RecordedCookieMode:
            if self.host_match(f.request.host):
                hid = (f.request.host, f.request.port)
                if hid in self.cookies:
                    f.request.headers.set_all('Cookie', self.cookies[hid])
        elif self.cookie_mode == AuditMaster.ManualCookieMode:
            self.flow_plugin.request(f)
        elif self.cookie_mode == AuditMaster.RemoveAllCookieMode:
            self.flow_plugin.remove_session(f)

        super(AuditMaster, self).handle_request(f)

    def handle_response(self, f):
        if self.cookie_mode == AuditMaster.RecordedCookieMode:
            if re.search(self.host, f.request.host):
                hid = (f.request.host, f.request.port)
                if 'set-cookie' in f.response.headers:
                    self.cookies[hid] = f.response.headers.get_all('set-cookie')
        if self.flow_plugin.response(f):
            requests = self.flow_plugin.format_request(f.request)
            content_type = ''
            if 'content-type' in f.response.headers:
                content_type = f.response.headers['content-type']

            groupingId = ''
            if self.group_plugin is not None:
                groupingId = '{0}:{1}'.format(self.group_plugin.current_group.id, self.group_plugin.current_group.name)

            result = FlowResult(self.flow_plugin.current_flow_id,
                                groupingId,
                                requests,
                                f.response.status_code,
                                f.response.get_decoded_content(),
                                content_type)
            self.flow_plugin.current_flow_id += 1
            self.results.append(result)
        super(AuditMaster, self).handle_response(f)


class AuditManager(object):

        def __init__(self, options):
            self.options = options
            self.results = []
            self.group_plugin = Plugin.load_plugin(options.group)
            self.flow_plugin = Plugin.load_plugin(options.flow)
            if self.flow_plugin is None:
                self.flow_plugin = FlowPlugin()
        if not os.path.exists('output'):
            os.makedirs('output')

        def record(self):
            m = RecordMaster(self.options)

            try:
                print 'Recording flows to the file {0}'.format(self.options.replay_file)
                print 'Only accepting requests that match the following criteria'
                print '\t- URI matching the regex {0}'.format(self.options.record_uri_filter)
                print '\t- Content type matching the regex {0}'.format(self.options.record_content_type)
                print '\t- Hosts matching the regex {0}'.format(self.options.host)
                m.run()
            except Exception as ex:
                print 'Unexpected error has occurred: ', ex.args
            finally:
                m.shutdown()

        def audit(self):

            results = []
            groups = []

            print 'Starting to audit flows from the file {0}'.format(self.options.replay_file)
            print 'Flow plugin: {0}'.format(type(self.flow_plugin))

            if self.group_plugin is not None:
                groups = self.group_plugin.get_list()
                print 'Group plugin: {0}'.format(type(self.group_plugin))
            else:
                print 'Group plugin: None configured'
                groups.append(Group(None, ''))
            print 'Cookie mode: {0}'.format(self.options.cookie_mode)

            for user_group in groups:

                if self.group_plugin is not None:
                    self.group_plugin.change_group(self.options.userid, user_group)
                    self.group_plugin.current_group = user_group

                m = AuditMaster(self.group_plugin,
                                self.flow_plugin,
                                self.options.host,
                                self.options.replay_file,
                                self.options.cookie_mode)
                try:
                    m.run()
                except Exception as ex:
                    print 'Unexpected error has occurred: ', ex.args
                    continue
                finally:
                    results.extend(m.results)
                    m.shutdown()

            return results

        def report(self, results):

            workbook_name = 'output/{0}.xlsx'.format(self.options.replay_file)
            print 'Creating the report, {0}, for {1} results'.format(workbook_name, len(results))

            if results is None:
                print
                'Nothing found to report on'
                return

            # Examine the results to create a unique list of column names for the report
            col_names = {result.report_col_name for result in results}

            workbook = xlsxwriter.Workbook(workbook_name)
            worksheet = workbook.add_worksheet()

            hdr_fmt = workbook.add_format({'bold': True, 'valign': 'vcenter', 'align': 'center', 'font_name':'Arial'})
            row_fmt = workbook.add_format({'valign': 'top', 'text_wrap' : '1', 'font_name':'Arial', 'font_size':'9'})

            worksheet.set_row(0, 45, hdr_fmt)
            worksheet.set_column('A:Z', 50)
            worksheet.write(0, 0, 'Recorded Request')

            row = 0
            col = 1

            for col_name in col_names:
                worksheet.write(row, col, col_name)
                col += 1

            unique_flows = {}

            for result in results:

                if result.flow_id in unique_flows:
                    continue

                unique_flows[result.flow_id] = result.flow_id

                col = 0
                row += 1
                worksheet.set_row(row, 30, row_fmt)

                # Move across and down so that flow id's match across groups
                match_results = [f for f in results if f.flow_id == result.flow_id]
                worksheet.write(row, col, result.path)

                for match_result in match_results:
                    col += 1
                    try:
                        value = self.flow_plugin.format_result(match_result, match_result.report_col_name)
                        if value is not None:
                             value = value.decode('utf-8','ignore')
                             worksheet.write(row, col, value)
                    except Exception as ex:
                        print 'Unexpected error formatting result: ', ex.args
                        continue

                os.system('clear')
            workbook.close()


