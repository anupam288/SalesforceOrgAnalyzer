"""
tests/test_parsers.py

Unit tests for the Apex and Flow parsers.
Run with: pytest tests/ -v
"""
import pytest
from parsers.flow_parser import FlowParser
from parsers.apex_parser import ApexParser


# ── FLOW PARSER TESTS ─────────────────────────────────────────────────

SAMPLE_FLOW_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>59.0</apiVersion>
    <label>Loan Application Stage Transition</label>
    <processType>AutoLaunchedFlow</processType>
    <start>
        <locationX>176</locationX>
        <locationY>0</locationY>
        <object>LoanApplication__c</object>
        <recordTriggerType>Update</recordTriggerType>
        <triggerType>RecordAfterSave</triggerType>
        <filters>
            <field>Status__c</field>
            <operator>EqualTo</operator>
            <value>
                <stringValue>Submitted</stringValue>
            </value>
        </filters>
        <connector>
            <targetReference>Check_Loan_Amount</targetReference>
        </connector>
    </start>
    <decisions>
        <name>Check_Loan_Amount</name>
        <label>Check Loan Amount</label>
        <locationX>264</locationX>
        <locationY>134</locationY>
        <rules>
            <name>High_Value_Loan</name>
            <label>High Value Loan</label>
            <conditionLogic>and</conditionLogic>
            <conditions>
                <leftValueReference>$Record.LoanAmount__c</leftValueReference>
                <operator>GreaterThan</operator>
                <rightValue>
                    <numberValue>10000000</numberValue>
                </rightValue>
            </conditions>
            <connector>
                <targetReference>Route_to_Credit_Committee</targetReference>
            </connector>
        </rules>
    </decisions>
    <actionCalls>
        <name>Route_to_Credit_Committee</name>
        <label>Route to Credit Committee</label>
        <actionName>SendToCreditCommittee</actionName>
        <actionType>apex</actionType>
        <inputParameters>
            <name>loanId</name>
            <value>
                <elementReference>$Record.Id</elementReference>
            </value>
        </inputParameters>
    </actionCalls>
</Flow>"""


class TestFlowParser:
    def setup_method(self):
        self.parser = FlowParser()

    def test_parse_trigger_type(self):
        result = self.parser.parse("Test_Flow", SAMPLE_FLOW_XML)
        assert result.trigger_type == "RecordAfterSave"

    def test_parse_trigger_object(self):
        result = self.parser.parse("Test_Flow", SAMPLE_FLOW_XML)
        assert result.trigger_object == "LoanApplication__c"

    def test_parse_entry_conditions(self):
        result = self.parser.parse("Test_Flow", SAMPLE_FLOW_XML)
        assert len(result.entry_conditions) > 0
        assert any("Status__c" in c for c in result.entry_conditions)

    def test_parse_nodes(self):
        result = self.parser.parse("Test_Flow", SAMPLE_FLOW_XML)
        assert "Check_Loan_Amount" in result.nodes
        assert "Route_to_Credit_Committee" in result.nodes

    def test_pseudocode_generated(self):
        result = self.parser.parse("Test_Flow", SAMPLE_FLOW_XML)
        assert len(result.pseudocode) > 50
        assert "LoanApplication__c" in result.pseudocode

    def test_empty_xml(self):
        result = self.parser.parse("Empty_Flow", "")
        assert result.api_name == "Empty_Flow"
        assert result.pseudocode == "[No source available]"

    def test_invalid_xml(self):
        result = self.parser.parse("Bad_Flow", "not valid xml at all")
        assert "error" in result.pseudocode.lower()


# ── APEX PARSER TESTS ─────────────────────────────────────────────────

SAMPLE_APEX_CLASS = """
/**
 * LoanApplicationService - Handles core loan application processing
 * Routes applications to appropriate credit review queues based on amount
 */
public with sharing class LoanApplicationService {
    
    // Maximum loan amount for relationship manager approval (50 lakhs)
    private static final Decimal RM_APPROVAL_LIMIT = 5000000;
    
    /**
     * Process a submitted loan application.
     * Validates FOIR, checks bureau score, and routes for approval.
     */
    public static void processApplication(Id applicationId) {
        // Fetch application with all related data
        LoanApplication__c app = [
            SELECT Id, LoanAmount__c, ApplicantIncome__c, Status__c, 
                   BureauScore__c, FOIR__c, AssignedOfficer__r.Name
            FROM LoanApplication__c
            WHERE Id = :applicationId
            LIMIT 1
        ];
        
        // FOIR check - reject if monthly obligations exceed 60% of income
        if (app.FOIR__c > 60) {
            app.Status__c = 'Rejected';
            app.RejectionReason__c = 'FOIR exceeds 60% threshold';
            update app;
            return;
        }
        
        // Route based on loan amount
        if (app.LoanAmount__c > RM_APPROVAL_LIMIT) {
            routeToCreditCommittee(app);
        } else {
            routeToRelationshipManager(app);
        }
    }
    
    private static void routeToCreditCommittee(LoanApplication__c app) {
        // Call external credit committee API
        HttpRequest req = new HttpRequest();
        req.setEndpoint('callout:CreditCommitteeAPI/applications');
        req.setMethod('POST');
        // ... send application data
        Http http = new Http();
        HttpResponse res = http.send(req);
        
        if (res.getStatusCode() == 200) {
            app.Status__c = 'With Credit Committee';
            update app;
        }
    }
    
    private static void routeToRelationshipManager(LoanApplication__c app) {
        app.Status__c = 'Pending RM Review';
        insert new Task(
            WhatId = app.Id,
            Subject = 'Review loan application: ' + app.Name,
            ActivityDate = Date.today().addDays(1),
            OwnerId = app.AssignedOfficer__c
        );
        update app;
    }
}
"""

SAMPLE_APEX_TRIGGER = """
trigger LoanApplicationTrigger on LoanApplication__c (before insert, before update, after insert, after update) {
    if (Trigger.isBefore) {
        if (Trigger.isInsert) {
            LoanApplicationHandler.onBeforeInsert(Trigger.new);
        } else if (Trigger.isUpdate) {
            LoanApplicationHandler.onBeforeUpdate(Trigger.new, Trigger.oldMap);
        }
    }
    if (Trigger.isAfter) {
        if (Trigger.isInsert) {
            LoanApplicationHandler.onAfterInsert(Trigger.new);
        } else if (Trigger.isUpdate) {
            LoanApplicationHandler.onAfterUpdate(Trigger.new, Trigger.oldMap);
        }
    }
}
"""


class TestApexParser:
    def setup_method(self):
        self.parser = ApexParser()

    def test_parse_class_type(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        assert result.class_type == "class"

    def test_parse_trigger_type(self):
        result = self.parser.parse("LoanApplicationTrigger", SAMPLE_APEX_TRIGGER)
        assert result.class_type == "trigger"

    def test_parse_trigger_objects(self):
        result = self.parser.parse("LoanApplicationTrigger", SAMPLE_APEX_TRIGGER)
        assert "LoanApplication__c" in result.trigger_objects

    def test_parse_trigger_events(self):
        result = self.parser.parse("LoanApplicationTrigger", SAMPLE_APEX_TRIGGER)
        assert any("before insert" in e.lower() or "before" in e.lower()
                   for e in result.trigger_events)

    def test_extract_soql(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        assert len(result.all_soql) > 0
        assert any("LoanApplication__c" in q for q in result.all_soql)

    def test_extract_dml(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        assert len(result.all_dml) > 0
        dml_ops = [op.upper() for op in result.all_dml]
        assert any("UPDATE" in op or "INSERT" in op for op in dml_ops)

    def test_extract_callouts(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        assert len(result.all_callouts) > 0
        assert any("CreditCommitteeAPI" in c for c in result.all_callouts)

    def test_extract_comments(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        assert len(result.all_comments) > 0
        # The FOIR comment should be captured
        comments_text = " ".join(result.all_comments)
        assert "FOIR" in comments_text or "foir" in comments_text.lower()

    def test_is_not_test_class(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        assert result.is_test is False

    def test_summary_dict(self):
        result = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        summary = result.to_summary_dict()
        assert summary["api_name"] == "LoanApplicationService"
        assert "soql_queries" in summary
        assert "external_callouts" in summary

    def test_empty_source(self):
        result = self.parser.parse("EmptyClass", "")
        assert result.class_type == "unknown"
        assert result.all_soql == []

    def test_build_call_graph(self):
        parsed1 = self.parser.parse("LoanApplicationService", SAMPLE_APEX_CLASS)
        parsed2 = self.parser.parse("LoanApplicationTrigger", SAMPLE_APEX_TRIGGER)
        graph = self.parser.build_call_graph([parsed1, parsed2])
        assert "LoanApplicationService" in graph
        assert "LoanApplicationTrigger" in graph
